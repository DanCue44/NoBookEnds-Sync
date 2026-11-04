"""Converted EPUB copies of AZW3 and MOBI ebooks, cached under /data/converted.

One file per ebook item, named after the ebook file's inode, size and change
time plus the converter version, so a replaced file or a newer converter makes
a new copy and the old one is removed. The alignment job and the app read the
same copy, which keeps the positions in an alignment valid for what the app
shows.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path

from .kindle import CONVERTER_VERSION, KINDLE_FORMATS, convert_to_epub

_SAFE = re.compile(r"[^A-Za-z0-9_-]")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def is_kindle(fmt: str | None) -> bool:
    return (fmt or "").lower() in KINDLE_FORMATS


def cache_key(ebook_id: str, eb: dict) -> str:
    """Names this ebook file's converted copy; changes when the file or converter does."""
    raw = f"{eb.get('ino')}|{eb.get('size')}|{eb.get('mtime')}|{CONVERTER_VERSION}"
    return f"{_SAFE.sub('_', ebook_id)}-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def cached_path(converted_dir: Path, ebook_id: str, eb: dict) -> Path:
    return converted_dir / f"{cache_key(ebook_id, eb)}.epub"


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def ensure_converted(converted_dir: Path, ebook_id: str, eb: dict, fetch) -> Path:
    """The converted EPUB for this ebook file, made from `fetch()` (its bytes) if not cached.

    Raises kindle.DrmProtected or kindle.KindleError when it cannot be made.
    """
    path = cached_path(converted_dir, ebook_id, eb)
    if path.exists():
        return path
    with _lock(ebook_id):
        if path.exists():
            return path
        epub = convert_to_epub(fetch())
        converted_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(epub)
        os.replace(tmp, path)
        prefix = f"{_SAFE.sub('_', ebook_id)}-"
        for old in converted_dir.glob(f"{prefix}*.epub"):
            if old != path and old.name[len(prefix):-5].isalnum():
                old.unlink(missing_ok=True)
    return path
