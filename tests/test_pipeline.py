"""Job pipeline with fake ABS and fake transcription: per-track transcript
cache, global time offsets, duration limit, mismatch detection."""

import shutil

import pytest

from nobookends_sync import pipeline
from nobookends_sync.config import Settings
from nobookends_sync.db import DB
from nobookends_sync.results import load_result
from nobookends_sync.transcriber import Word

AUDIO, EBOOK = "aud", "ebk"


@pytest.fixture
def setup(tmp_path, epub_path, book, monkeypatch):
    s = Settings()
    s.data_dir = tmp_path
    s.ensure_dirs()
    db = DB(s.db_path)
    db.insert_job(AUDIO, EBOOK, priority=10, source="test", sort_key=0)

    # narration: every book token, 0.4 s apart, split over 3 files of 60 s
    words = [(book.tokens[j], 2.0 + 0.4 * j) for j in range(len(book.tokens))]
    tracks = [{"index": i + 1, "ino": f"ino{i}", "duration": 60.0, "startOffset": 60.0 * i,
               "contentUrl": f"/f/{i}", "metadata": {"ext": ".m4a", "size": 1000 + i, "mtimeMs": 1}}
              for i in range(3)]
    audio_item = {"id": AUDIO, "media": {"tracks": tracks, "audioFiles": []}}
    ebook_item = {"id": EBOOK, "media": {"ebookFile": {"ino": "e", "ebookFormat": "epub",
                                                       "metadata": {"size": 5, "mtimeMs": 1}}}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get_item(self, item_id, expanded=True):
            return audio_item if item_id == AUDIO else ebook_item

        def download_ebook(self, item_id, dest):
            shutil.copy(epub_path, dest)

        def download_track(self, item_id, track, dest):
            dest.write_text(track["ino"])

        def close(self):
            pass

    calls = []

    class FakeTranscriber:
        device, compute_type = "cpu", "int8"

        def __init__(self, settings):
            pass

    def fake_transcribe_file(transcriber, src, track, need, on_progress, **kw):
        calls.append((track["ino"], need))
        lo, hi = track["start"], track["start"] + (need or track["duration"])
        return [Word(t, a - track["start"], a - track["start"] + 0.25, 0.9) for t, a in words if lo <= a < hi]

    monkeypatch.setattr(pipeline, "ServiceClient", FakeClient)
    monkeypatch.setattr(pipeline, "_transcribe_file", fake_transcribe_file)
    import nobookends_sync.transcriber as tmod

    monkeypatch.setattr(tmod, "Transcriber", FakeTranscriber)
    monkeypatch.setattr(pipeline, "MISMATCH_CHECK_TOKENS", 10**9)
    return s, db, calls, book


def test_full_run_then_cached_rerun(setup):
    s, db, calls, book = setup
    pipeline.run_job(s, AUDIO, EBOOK)
    job = db.get_job(AUDIO, EBOOK)
    assert job["status"] == "ready", job["error"]
    assert len(calls) == 3
    res = load_result(s.results_dir, AUDIO, EBOOK)
    segs = res.segments
    assert res.doc["durationSec"] == 180.0
    # times are global across files: the first word of every sentence keeps its offset
    for seg in segs:
        j = book.sentences[seg["id"]].tok_first
        assert abs(seg["t0"] - (2.0 + 0.4 * j)) < 0.01
    assert any(seg["t0"] > 120 for seg in segs)  # third file contributes
    assert job["quality"]["score"] > 0.9

    # re-running (e.g. after an algorithm bump) never re-transcribes
    db.update_job(AUDIO, EBOOK, status="processing")
    pipeline.run_job(s, AUDIO, EBOOK)
    assert len(calls) == 3
    assert db.get_job(AUDIO, EBOOK)["status"] == "ready"


def test_duration_limit_then_full_run_reuses_cache(setup):
    s, db, calls, book = setup
    db.update_job(AUDIO, EBOOK, max_duration=90.0)
    pipeline.run_job(s, AUDIO, EBOOK)
    assert db.get_job(AUDIO, EBOOK)["status"] == "ready"
    assert calls == [("ino0", None), ("ino1", 30.0)]
    res = load_result(s.results_dir, AUDIO, EBOOK)
    assert max(seg["t1"] for seg in res.segments) <= 91
    assert res.doc["processedSec"] == 90.0

    db.update_job(AUDIO, EBOOK, max_duration=None)
    pipeline.run_job(s, AUDIO, EBOOK)
    # file 0 is cached; file 1 was only partly done so it is redone; file 2 is new
    assert calls[2:] == [("ino1", None), ("ino2", None)]


def test_wrong_book_fails_early(setup, monkeypatch):
    s, db, calls, book = setup
    monkeypatch.setattr(pipeline, "MISMATCH_CHECK_TOKENS", 50)

    def gibberish(transcriber, src, track, need, on_progress, **kw):
        calls.append(track["ino"])
        return [Word(f"zz{k}", k * 0.5, k * 0.5 + 0.3, 0.9) for k in range(100)]

    monkeypatch.setattr(pipeline, "_transcribe_file", gibberish)
    pipeline.run_job(s, AUDIO, EBOOK)
    job = db.get_job(AUDIO, EBOOK)
    assert job["status"] == "failed" and "does not match" in job["error"]
    assert calls == ["ino0"]  # stopped after the first file


# ------------------------------------------------------------ checkpoints


def _fake_audio(monkeypatch, duration, words):
    """iter_chunks over a fake file of `duration` seconds with 30 s chunks and
    2 s overlap; the fake transcriber returns the true words inside each chunk."""
    import numpy as np

    from nobookends_sync import audio
    from nobookends_sync.audio import SR

    def fake_iter_chunks(path, chunk_sec=30.0, overlap_sec=2.0, max_sec=None, start_sec=0.0):
        end = min(duration, max_sec) if max_sec else duration
        start = start_sec
        while start < end:
            n = min(chunk_sec, end - start)
            yield start, np.zeros(int(n * SR), dtype=np.float32)
            if start + n >= end:
                return
            start += chunk_sec - overlap_sec

    class FakeTranscriber:
        def transcribe(self, samples, on_progress=None):
            span = len(samples) / SR
            lo = self.offset
            return [Word(t, a - lo, b - lo, 0.9) for t, a, b in words if a >= lo and b <= lo + span]

    monkeypatch.setattr(audio, "OVERLAP_SEC", 2.0)
    tr = FakeTranscriber()
    real = fake_iter_chunks

    def tracking_iter(*a, **k):
        for start, samples in real(*a, **k):
            tr.offset = start
            yield start, samples

    monkeypatch.setattr(audio, "iter_chunks", tracking_iter)
    return tr


def test_transcribe_resumes_from_checkpoint_with_same_words(monkeypatch, tmp_path):
    words = [(f"w{k}", k * 0.7, k * 0.7 + 0.4) for k in range(140)]  # about 98 s of speech
    tr = _fake_audio(monkeypatch, 100.0, words)
    track = {"index": 1, "start": 0.0, "duration": 100.0}
    full = pipeline._transcribe_file(tr, tmp_path / "x", track, None, lambda x: None)
    assert [w.text for w in full] == [w for w, _, _ in words]

    checkpoints = []

    class Stop(Exception):
        pass

    def on_checkpoint(ws, covered):
        checkpoints.append((len(ws), covered))
        if len(checkpoints) == 2:
            saved.extend(ws)
            raise Stop()

    saved = []
    with pytest.raises(Stop):
        pipeline._transcribe_file(tr, tmp_path / "x", track, None, lambda x: None, on_checkpoint=on_checkpoint)
    assert checkpoints[0][1] == 28.0 and checkpoints[1][1] == 56.0
    resumed = pipeline._transcribe_file(tr, tmp_path / "x", track, None, lambda x: None,
                                        resume_from=56.0, prior=saved)
    assert [w.text for w in resumed] == [w.text for w in full]
    assert [round(w.t0, 3) for w in resumed] == [round(w.t0, 3) for w in full]


def test_paused_job_keeps_checkpoint_and_resumes(setup, monkeypatch):
    s, db, _calls, book = setup
    seen = []

    def checkpointed(transcriber, src, track, need, on_progress, resume_from=0.0, prior=None, on_checkpoint=None):
        seen.append((track["ino"], resume_from, len(prior or [])))
        lo = track["start"]
        mine = [Word(t, a - lo, a - lo + 0.25, 0.9) for t, a in
                ((book.tokens[j], 2.0 + 0.4 * j) for j in range(len(book.tokens))) if lo <= a < lo + 60]
        first = [w for w in mine if w.t0 < 30]
        if resume_from == 0.0:
            if track["ino"] == "ino1":
                pipeline.request_pause(db, AUDIO, EBOOK)
            on_checkpoint(first, 30.0)  # raises JobPaused when a pause was requested
        return mine

    monkeypatch.setattr(pipeline, "_transcribe_file", checkpointed)
    pipeline.run_job(s, AUDIO, EBOOK)
    job = db.get_job(AUDIO, EBOOK)
    assert job["status"] == "queued" and not job["error"]
    assert seen == [("ino0", 0.0, 0), ("ino1", 0.0, 0)]

    pipeline.clear_pause(db)
    db.update_job(AUDIO, EBOOK, status="processing")
    pipeline.run_job(s, AUDIO, EBOOK)
    assert db.get_job(AUDIO, EBOOK)["status"] == "ready"
    # file 0 came from the cache, file 1 resumed at its 30 s checkpoint, file 2 started fresh
    assert seen[2][:2] == ("ino1", 30.0) and seen[2][2] > 0
    assert seen[3] == ("ino2", 0.0, 0)


def test_kindle_ebook_is_converted_and_aligned(tmp_path, monkeypatch):
    """An AZW3 ebook: converted once, cached, and aligned against that EPUB."""
    import io
    from pathlib import Path

    from nobookends_sync import kindle
    from nobookends_sync.converted import cached_path
    from nobookends_sync.epub import load_epub

    azw3 = Path(__file__).parent / "fixtures" / "kindle" / "alice.azw3"
    converted = load_epub(io.BytesIO(kindle.convert_to_epub(azw3.read_bytes())))
    s = Settings()
    s.data_dir = tmp_path
    s.ensure_dirs()
    db = DB(s.db_path)
    db.insert_job(AUDIO, EBOOK, priority=10, source="test", sort_key=0)
    words = [(converted.tokens[j], 2.0 + 0.4 * j) for j in range(len(converted.tokens))]
    tracks = [{"index": 1, "ino": "ino0", "duration": 400.0, "startOffset": 0.0, "contentUrl": "/f/0",
               "metadata": {"ext": ".m4a", "size": 1000, "mtimeMs": 1}}]
    eb = {"ino": "e", "ebookFormat": "azw3", "metadata": {"size": 5, "mtimeMs": 1}}
    audio_item = {"id": AUDIO, "media": {"tracks": tracks, "audioFiles": []}}
    ebook_item = {"id": EBOOK, "media": {"ebookFile": eb}}
    downloads = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get_item(self, item_id, expanded=True):
            return audio_item if item_id == AUDIO else ebook_item

        def download_ebook(self, item_id, dest):
            downloads.append(item_id)
            shutil.copy(azw3, dest)

        def download_track(self, item_id, track, dest):
            dest.write_text(track["ino"])

        def close(self):
            pass

    def fake_transcribe_file(transcriber, src, track, need, on_progress, **kw):
        return [Word(t, a, a + 0.25, 0.9) for t, a in words if a < (need or track["duration"])]

    import nobookends_sync.transcriber as tmod

    class FakeTranscriber:
        device, compute_type = "cpu", "int8"

        def __init__(self, settings):
            pass

    monkeypatch.setattr(pipeline, "ServiceClient", FakeClient)
    monkeypatch.setattr(pipeline, "_transcribe_file", fake_transcribe_file)
    monkeypatch.setattr(tmod, "Transcriber", FakeTranscriber)
    monkeypatch.setattr(pipeline, "MISMATCH_CHECK_TOKENS", 10**9)

    pipeline.run_job(s, AUDIO, EBOOK)
    job = db.get_job(AUDIO, EBOOK)
    assert job["status"] == "ready", job["error"]
    assert job["quality"]["score"] > 0.9
    eb_info = {"ino": "e", "size": 5, "mtime": 1}
    assert cached_path(s.converted_dir, EBOOK, eb_info).exists()
    db.update_job(AUDIO, EBOOK, status="processing")
    pipeline.run_job(s, AUDIO, EBOOK)
    assert downloads == [EBOOK]  # the second run read the cached copy

    s.kindle_ebooks = False
    db.update_job(AUDIO, EBOOK, status="processing")
    pipeline.run_job(s, AUDIO, EBOOK)
    assert db.get_job(AUDIO, EBOOK)["status"] == "failed"
