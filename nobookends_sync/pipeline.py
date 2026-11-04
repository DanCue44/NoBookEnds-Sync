"""One alignment job: fetch, transcribe (cached per track), align, store.

Runs in a child process so that CUDA memory is released between jobs and the
API process never blocks on CPU-heavy alignment.

Transcripts are checkpointed after every audio chunk (20 minutes of audio), so
a job that is stopped or paused resumes from the last checkpoint, even inside a
long single-file audiobook. Checkpoints are also where a job honors a pause
request (an auto job at the end of the overnight window).
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import shutil
import time
from pathlib import Path

from . import ALGO_VERSION, SCHEMA_VERSION, __version__
from .abs_client import ServiceClient, audio_tracks, ebook_file, file_signature
from .aligner import align_book, asr_tokens_from_words, count_anchor_hits
from .config import Settings
from .converted import ensure_converted, is_kindle
from .db import DB
from .epub import load_epub
from .kindle import DrmProtected, KindleError
from .results import save_result
from .transcriber import Word

log = logging.getLogger("nbs.pipeline")

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")

MISMATCH_CHECK_TOKENS = 3000
MISMATCH_MIN_HITS = 5


PAUSE_KEY = "worker.pause"


class JobFailed(Exception):
    pass


class JobPaused(Exception):
    """Raised at a checkpoint when the worker asked this job to pause."""


def request_pause(db: DB, audio_id: str, ebook_id: str):
    db.kv_set(PAUSE_KEY, [audio_id, ebook_id])


def clear_pause(db: DB):
    db.kv_set(PAUSE_KEY, None)


def pause_requested(db: DB, audio_id: str, ebook_id: str) -> bool:
    return db.kv_get(PAUSE_KEY) == [audio_id, ebook_id]


def mark_paused(db: DB, audio_id: str, ebook_id: str):
    """Back to the queue; progress and transcript checkpoints are kept."""
    db.update_job(audio_id, ebook_id, status="queued")


def _model_slug(s: Settings) -> str:
    return _SAFE.sub("_", s.whisper_model)


def transcript_path(s: Settings, audio_id: str, track: dict) -> Path:
    return s.transcripts_dir / _SAFE.sub("_", audio_id) / _model_slug(s) / f"track-{int(track['index']):04d}.json.gz"


def load_cached_track(s: Settings, audio_id: str, track: dict, need_sec: float | None):
    """Cached words for a track if the file is unchanged and the cache covers
    what we need. Times in the cache are relative to the track start."""
    p = transcript_path(s, audio_id, track)
    if not p.exists():
        return None
    try:
        with gzip.open(p, "rt") as f:
            d = json.load(f)
    except Exception:
        return None
    if d.get("ino") != track["ino"] or d.get("size") != track["size"] or d.get("mtime") != track["mtime"]:
        return None
    covered = d.get("covered", 0.0)
    if not d.get("complete") and (need_sec is None or covered < need_sec):
        return None
    return d


def load_partial_track(s: Settings, audio_id: str, track: dict):
    """An unfinished transcript of an unchanged file (a checkpoint or a
    duration-limited run), to resume from. None when there is nothing usable."""
    p = transcript_path(s, audio_id, track)
    if not p.exists():
        return None
    try:
        with gzip.open(p, "rt") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if d.get("ino") != track["ino"] or d.get("size") != track["size"] or d.get("mtime") != track["mtime"]:
        return None
    if d.get("complete") or not d.get("covered"):
        return None
    return d


def save_cached_track(s: Settings, audio_id: str, track: dict, words, covered: float, complete: bool, meta: dict):
    p = transcript_path(s, audio_id, track)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump({
            "ino": track["ino"], "size": track["size"], "mtime": track["mtime"],
            "duration": track["duration"], "covered": covered, "complete": complete,
            "words": [[w.text, round(w.t0, 3), round(w.t1, 3), round(w.p, 3)] for w in words],
            **meta,
        }, f)
    tmp.replace(p)


def run_job(settings: Settings, audio_id: str, ebook_id: str):
    """Entry point for the worker child process."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    db = DB(settings.db_path)
    job = db.get_job(audio_id, ebook_id)
    if not job:
        return
    abs_ = ServiceClient(settings.abs_url, settings.abs_api_key)
    work = settings.tmp_dir / f"{_SAFE.sub('_', audio_id)}__{_SAFE.sub('_', ebook_id)}"
    t_start = time.time()
    try:
        _run(settings, db, abs_, job, work)
    except JobPaused:
        log.info("job %s/%s paused at a checkpoint", audio_id, ebook_id)
        mark_paused(db, audio_id, ebook_id)
    except Exception as e:  # noqa: BLE001
        log.exception("job %s/%s failed", audio_id, ebook_id)
        db.update_job(audio_id, ebook_id, status="failed", error=str(e)[:500], finished_at=time.time(),
                      keep_result=0)
    finally:
        abs_.close()
        shutil.rmtree(work, ignore_errors=True)
        log.info("job %s/%s finished in %.1fs", audio_id, ebook_id, time.time() - t_start)


def _run(s: Settings, db: DB, abs_: ServiceClient, job: dict, work: Path):
    audio_id, ebook_id = job["audio_id"], job["ebook_id"]
    shutil.rmtree(work, ignore_errors=True)  # leftovers from a preempted run
    work.mkdir(parents=True, exist_ok=True)
    stats: dict = {"model": s.whisper_model}

    audio_item = abs_.get_item(audio_id, expanded=True)
    ebook_item = audio_item if ebook_id == audio_id else abs_.get_item(ebook_id, expanded=True)
    tracks = audio_tracks(audio_item)
    if not tracks:
        raise JobFailed("audio item has no audio tracks")
    eb = ebook_file(ebook_item)
    kindle_book = bool(eb) and is_kindle(eb["format"])
    if not eb or (eb["format"] != "epub" and not (kindle_book and s.kindle_ebooks)):
        raise JobFailed("ebook item has no EPUB file")
    signature = file_signature(audio_item, ebook_item)
    book_duration = sum(t["duration"] for t in tracks)
    db.update_job(audio_id, ebook_id, duration_sec=book_duration, signature=signature)

    if kindle_book:
        # The same converted copy the app is sent (GET /api/v1/ebooks/{id}/epub)
        def fetch() -> bytes:
            raw = work / "book.kindle"
            abs_.download_ebook(ebook_id, raw)
            data = raw.read_bytes()
            raw.unlink(missing_ok=True)
            return data

        try:
            book = load_epub(ensure_converted(s.converted_dir, ebook_id, eb, fetch))
        except DrmProtected as e:
            raise JobFailed("the ebook is protected with DRM and cannot be read") from e
        except KindleError as e:
            raise JobFailed(f"the ebook could not be converted: {e}") from e
        stats["convertedFrom"] = eb["format"]
    else:
        epub_path = work / "book.epub"
        abs_.download_ebook(ebook_id, epub_path, eb.get("path"))
        book = load_epub(epub_path)
        epub_path.unlink(missing_ok=True)
    if len(book.tokens) < 50:
        raise JobFailed("EPUB has no usable text")
    stats["bookTokens"] = len(book.tokens)

    limit = job.get("max_duration") or s.max_audio_sec or None
    target = min(book_duration, limit) if limit else book_duration

    transcriber = None
    words_all = []      # (text, t0, t1) absolute
    done_sec = 0.0
    trans_wall = 0.0
    trans_audio = 0.0
    last_progress = [0.0]
    checked = False

    def progress(sec_done):
        p = min(0.95, 0.95 * sec_done / max(target, 1.0))
        if p - last_progress[0] >= 0.005:
            last_progress[0] = p
            db.update_job(audio_id, ebook_id, progress=round(p, 4))

    for tr in tracks:
        if limit and tr["start"] >= limit:
            break
        need = (limit - tr["start"]) if limit and tr["start"] + tr["duration"] > limit else None
        cached = load_cached_track(s, audio_id, tr, need)
        if cached is not None:
            for w, a, b, _p in cached["words"]:
                if need is None or a < need:
                    words_all.append((w, tr["start"] + a, tr["start"] + b))
        else:
            partial = load_partial_track(s, audio_id, tr)
            resume_from, prior = 0.0, []
            if partial and partial["covered"] < (need or tr["duration"]):
                resume_from = partial["covered"]
                prior = [Word(w, a, b, p) for w, a, b, p in partial["words"]]
                log.info("track %s: resuming transcription at %.0fs", tr["index"], resume_from)
            if transcriber is None:
                from .transcriber import Transcriber

                transcriber = Transcriber(s)
                stats.update(device=transcriber.device, computeType=transcriber.compute_type,
                             beamSize=s.whisper_beam_size)
            src = work / f"track{tr['ino']}{tr['ext'] or '.bin'}"
            abs_.download_track(audio_id, tr, src)
            t0 = time.time()

            def checkpoint(words_so_far, covered, tr=tr, need=need):
                save_cached_track(s, audio_id, tr, words_so_far, covered, False,
                                  {"model": s.whisper_model, "checkpoint": True})
                if pause_requested(db, audio_id, ebook_id):
                    raise JobPaused()

            words = _transcribe_file(transcriber, src, tr, need, lambda x, tr=tr: progress(tr["start"] + x),
                                     resume_from=resume_from, prior=prior, on_checkpoint=checkpoint)
            dt = time.time() - t0
            covered = need if need else tr["duration"]
            trans_wall += dt
            trans_audio += covered - resume_from
            src.unlink(missing_ok=True)
            save_cached_track(s, audio_id, tr, words, covered, need is None,
                              {"model": s.whisper_model, "wall": round(dt, 2)})
            words_all.extend((w.text, tr["start"] + w.t0, tr["start"] + w.t1) for w in words)
            if pause_requested(db, audio_id, ebook_id):
                raise JobPaused()  # between files is a safe point too
        done_sec = tr["start"] + (need if need else tr["duration"])
        progress(done_sec)

        if not checked and len(words_all) >= MISMATCH_CHECK_TOKENS:
            checked = True
            asr = asr_tokens_from_words(words_all)
            hits = count_anchor_hits([a.norm for a in asr], book.tokens)
            stats["earlyAnchorHits"] = hits
            if hits < MISMATCH_MIN_HITS:
                raise JobFailed("narration does not match this ebook (no shared passages found)")

    if trans_audio:
        stats["transcribeWallSec"] = round(trans_wall, 1)
        stats["transcribedAudioSec"] = round(trans_audio, 1)
        stats["realtimeFactor"] = round(trans_audio / max(trans_wall, 1e-6), 2)
    stats["audioSecProcessed"] = round(done_sec, 1)

    t0 = time.time()
    asr = asr_tokens_from_words(words_all)
    segments, quality = align_book(book, asr)
    stats["alignSec"] = round(time.time() - t0, 2)
    if quality["score"] < 0.2 and quality["matchedTokens"] < 200:
        raise JobFailed(f"alignment quality too low (score {quality['score']})")

    doc = {
        "version": SCHEMA_VERSION,
        "algoVersion": ALGO_VERSION,
        "service": __version__,
        "audioItemId": audio_id,
        "ebookItemId": ebook_id,
        "durationSec": round(book_duration, 3),
        "processedSec": round(done_sec, 3),
        "signature": signature,
        "quality": quality,
        "createdAt": time.time(),
        "segments": segments,
    }
    etag = save_result(s.results_dir, audio_id, ebook_id, doc)
    db.update_job(
        audio_id, ebook_id, status="ready", progress=1.0, finished_at=time.time(), error=None,
        quality=quality, stats=stats, signature=signature, result_algo=ALGO_VERSION, result_etag=etag,
        keep_result=0, duration_sec=book_duration,
    )


def _transcribe_file(transcriber, src: Path, track: dict, need: float | None, on_progress, *,
                     resume_from: float = 0.0, prior=None, on_checkpoint=None):
    """Transcribe a file in overlapping chunks; returns words relative to the file start.

    resume_from/prior continue an unfinished transcript: decoding restarts one
    overlap before the checkpoint and prior words near the checkpoint are
    redone, so the result matches an uninterrupted run. on_checkpoint(words,
    covered) is called after every chunk but the last; covered is where the
    next chunk starts."""
    from .audio import OVERLAP_SEC, SR, iter_chunks

    out = []
    start_at = 0.0
    if resume_from > 0:
        start_at = max(0.0, resume_from - OVERLAP_SEC)
        out = [w for w in (prior or []) if w.t1 <= start_at + 0.5]
    last_end = out[-1].t1 if out else -1.0
    pending = None  # checkpoint for the previous chunk, sent once a later chunk exists
    for start, samples in iter_chunks(str(src), max_sec=need, start_sec=start_at):
        if pending is not None and on_checkpoint:
            on_checkpoint(list(pending[0]), pending[1])
        pending = None
        words = transcriber.transcribe(samples, on_progress=lambda x: on_progress(start + x))
        for w in words:
            w.t0 += start
            w.t1 += start
            if w.t0 < last_end - 0.05:
                continue  # already covered by the previous chunk's overlap
            out.append(w)
        if out:
            last_end = out[-1].t1
        pending = (out, start + len(samples) / SR - OVERLAP_SEC)
    return out
