"""Chunked audio decoding (16 kHz mono float32) so multi-hour single-file
audiobooks never need to be held in memory at once."""

from __future__ import annotations

import numpy as np

SR = 16000


CHUNK_SEC = 1200.0
OVERLAP_SEC = 2.0


def iter_chunks(path: str, chunk_sec: float = CHUNK_SEC, overlap_sec: float = OVERLAP_SEC,
                max_sec: float | None = None, start_sec: float = 0.0):
    """Yield (start_sec, samples) chunks; consecutive chunks overlap by overlap_sec.

    start_sec skips the audio before that point (resuming a checkpointed
    transcription). The skipped part is still decoded, which is far cheaper
    than transcribing it, and keeps sample positions exact."""
    import av

    chunk_n = int(chunk_sec * SR)
    overlap_n = int(overlap_sec * SR)
    max_n = int(max_sec * SR) if max_sec else None
    skip_n = int(start_sec * SR) if start_sec and start_sec > 0 else 0
    buf: list[np.ndarray] = []
    buf_n = 0
    start_n = skip_n  # absolute sample index of buf[0]
    total = 0  # absolute samples decoded so far
    with av.open(path) as container:
        stream = next(s for s in container.streams if s.type == "audio")
        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=SR)

        def frames():
            for frame in container.decode(stream):
                yield from resampler.resample(frame)
            yield from resampler.resample(None)

        for fr in frames():
            arr = fr.to_ndarray().reshape(-1).astype(np.float32) / 32768.0
            first = total
            total += arr.size
            if total <= skip_n:
                continue
            if first < skip_n:
                arr = arr[skip_n - first:]
            buf.append(arr)
            buf_n += arr.size
            if max_n and total >= max_n:
                break
            if buf_n >= chunk_n:
                data = np.concatenate(buf)
                yield start_n / SR, data[:chunk_n]
                keep = data[chunk_n - overlap_n:]
                start_n += chunk_n - overlap_n
                buf, buf_n = [keep], keep.size
    if buf_n:
        data = np.concatenate(buf)
        if max_n:
            data = data[: max(0, max_n - start_n)]
        if data.size > SR // 2 or (start_n == skip_n and data.size):
            yield start_n / SR, data
