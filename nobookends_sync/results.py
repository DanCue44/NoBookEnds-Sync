"""Alignment results: one gzip-compressed JSON file per (audio item, ebook item)."""

from __future__ import annotations

import bisect
import gzip
import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def result_path(results_dir: Path, audio_id: str, ebook_id: str) -> Path:
    return results_dir / f"{_SAFE.sub('_', audio_id)}__{_SAFE.sub('_', ebook_id)}.json.gz"


def save_result(results_dir: Path, audio_id: str, ebook_id: str, doc: dict) -> str:
    """Write atomically. Returns the content hash used as the ETag base."""
    raw = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()
    etag = hashlib.sha256(raw).hexdigest()[:32]
    doc_path = result_path(results_dir, audio_id, ebook_id)
    tmp = doc_path.with_suffix(".tmp")
    with gzip.open(tmp, "wb", compresslevel=6) as f:
        f.write(raw)
    os.replace(tmp, doc_path)
    _cache.drop(str(doc_path))
    return etag


class LoadedResult:
    def __init__(self, doc: dict, etag: str):
        self.doc = doc
        self.etag = etag
        self.segments = doc.get("segments", [])
        self._t0 = [s["t0"] for s in self.segments]
        self._t1max = []
        m = float("-inf")
        for s in self.segments:
            m = max(m, s["t1"])
            self._t1max.append(m)

    def window(self, start: float | None, end: float | None) -> list[dict]:
        """Segments overlapping [start, end]."""
        if start is None and end is None:
            return self.segments
        lo = 0
        if start is not None:
            lo = bisect.bisect_left(self._t1max, start)
        hi = len(self.segments)
        if end is not None:
            hi = bisect.bisect_right(self._t0, end)
        return self.segments[lo:hi] if hi > lo else []


class _Cache:
    def __init__(self, size=4):
        self.size = size
        self.items: OrderedDict[str, tuple[float, LoadedResult]] = OrderedDict()
        self.lock = threading.Lock()

    def drop(self, key):
        with self.lock:
            self.items.pop(key, None)

    def get(self, path: Path) -> LoadedResult | None:
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return None
        with self.lock:
            hit = self.items.get(key)
            if hit and hit[0] == mtime:
                self.items.move_to_end(key)
                return hit[1]
        with gzip.open(path, "rb") as f:
            raw = f.read()
        lr = LoadedResult(json.loads(raw), hashlib.sha256(raw).hexdigest()[:32])
        with self.lock:
            self.items[key] = (mtime, lr)
            self.items.move_to_end(key)
            while len(self.items) > self.size:
                self.items.popitem(last=False)
        return lr


_cache = _Cache()


def load_result(results_dir: Path, audio_id: str, ebook_id: str) -> LoadedResult | None:
    return _cache.get(result_path(results_dir, audio_id, ebook_id))


def delete_result(results_dir: Path, audio_id: str, ebook_id: str):
    p = result_path(results_dir, audio_id, ebook_id)
    _cache.drop(str(p))
    p.unlink(missing_ok=True)
