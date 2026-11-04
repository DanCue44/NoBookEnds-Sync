"""Audiobookshelf access.

* ServiceClient (sync): the service's own read-only API key, used to list
  libraries and download audio/ebook files.
* UserAuth (async): validates a caller's ABS access token with GET /api/me and
  checks item access by fetching the item with the caller's own token.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import httpx

from .kindle import CONVERTER_VERSION, KINDLE_FORMATS

TIMEOUT = httpx.Timeout(30.0, read=300.0)


class AbsError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(f"ABS {status}: {message}")
        self.status = status


# ------------------------------------------------------------------ helpers


def audio_tracks(item: dict) -> list[dict]:
    """Ordered audio tracks with the global start offset of each file."""
    media = item.get("media") or {}
    tracks = media.get("tracks") or []
    files = {str(f.get("ino")): f for f in media.get("audioFiles") or []}
    out = []
    if tracks:
        for t in sorted(tracks, key=lambda t: t.get("index", 0)):
            f = files.get(str(t.get("ino"))) or {}
            md = t.get("metadata") or f.get("metadata") or {}
            out.append({
                "index": t.get("index"),
                "ino": str(t.get("ino")),
                "duration": float(t.get("duration") or 0),
                "start": float(t.get("startOffset") or 0),
                "content_url": t.get("contentUrl"),
                "ext": md.get("ext") or "",
                "size": md.get("size"),
                "mtime": md.get("mtimeMs"),
            })
    else:  # fall back to audioFiles in index order
        start = 0.0
        for f in sorted(files.values(), key=lambda f: f.get("index", 0)):
            if f.get("exclude") or f.get("invalid"):
                continue
            md = f.get("metadata") or {}
            dur = float(f.get("duration") or 0)
            out.append({
                "index": f.get("index"),
                "ino": str(f.get("ino")),
                "duration": dur,
                "start": start,
                "content_url": f"/api/items/{item['id']}/file/{f.get('ino')}",
                "ext": md.get("ext") or "",
                "size": md.get("size"),
                "mtime": md.get("mtimeMs"),
            })
            start += dur
    return out


def ebook_file(item: dict) -> dict | None:
    ef = (item.get("media") or {}).get("ebookFile")
    if not ef:
        return None
    md = ef.get("metadata") or {}
    return {
        "ino": str(ef.get("ino")),
        "format": (ef.get("ebookFormat") or md.get("ext", "").lstrip(".")).lower(),
        "size": md.get("size"),
        "mtime": md.get("mtimeMs"),
    }


def file_signature(audio_item: dict, ebook_item: dict) -> str:
    parts = [[t["ino"], t["size"], t["mtime"], round(t["duration"], 2)] for t in audio_tracks(audio_item)]
    eb = ebook_file(ebook_item) or {}
    e = [eb.get("ino"), eb.get("size"), eb.get("mtime")]
    if eb.get("format") in KINDLE_FORMATS:
        e.append(f"kindle{CONVERTER_VERSION}")
    raw = json.dumps({"a": parts, "e": e}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _check(r: httpx.Response):
    if r.status_code >= 400:
        raise AbsError(r.status_code, r.text[:200])


# ------------------------------------------------------------------ service


class ServiceClient:
    def __init__(self, base_url: str, api_key: str, transport: httpx.BaseTransport | None = None):
        self.base = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT,
            transport=transport,
            follow_redirects=True,
        )

    def close(self):
        self.client.close()

    def get_item(self, item_id: str, expanded: bool = True) -> dict:
        r = self.client.get(f"/api/items/{item_id}", params={"expanded": 1} if expanded else None)
        _check(r)
        return r.json()

    def libraries(self) -> list[dict]:
        r = self.client.get("/api/libraries")
        _check(r)
        d = r.json()
        return d.get("libraries", d if isinstance(d, list) else [])

    def iter_library_items(self, library_id: str, sort: str = "updatedAt", desc: bool = True, page_size: int = 200):
        page = 0
        while True:
            r = self.client.get(
                f"/api/libraries/{library_id}/items",
                params={"sort": sort, "desc": 1 if desc else 0, "limit": page_size, "page": page},
            )
            _check(r)
            d = r.json()
            results = d.get("results") or []
            yield from results
            if len(results) < page_size:
                return
            page += 1

    def download(self, path: str, dest: Path, chunk: int = 1 << 20) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with self.client.stream("GET", path) as r:
            _check(r)
            with open(dest, "wb") as f:
                for b in r.iter_bytes(chunk):
                    f.write(b)
                    n += len(b)
        return n

    def download_track(self, item_id: str, track: dict, dest: Path) -> int:
        try:
            return self.download(track["content_url"] or f"/api/items/{item_id}/file/{track['ino']}", dest)
        except AbsError:
            return self.download(f"/api/items/{item_id}/file/{track['ino']}/download", dest)

    def download_ebook(self, item_id: str, dest: Path) -> int:
        return self.download(f"/api/items/{item_id}/ebook", dest)


# ------------------------------------------------------------------ user auth


class _TTLCache:
    def __init__(self, ttl: float, max_items: int = 2000):
        self.ttl = ttl
        self.max = max_items
        self.d: dict = {}

    def get(self, key):
        v = self.d.get(key)
        if v and v[0] > time.monotonic():
            return v[1]
        self.d.pop(key, None)
        return None

    def set(self, key, value):
        if len(self.d) >= self.max:
            now = time.monotonic()
            self.d = {k: v for k, v in self.d.items() if v[0] > now}
            if len(self.d) >= self.max:
                self.d.clear()
        self.d[key] = (time.monotonic() + self.ttl, value)


def token_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class UserAuth:
    def __init__(self, base_url: str, ttl: float = 60.0, transport: httpx.AsyncBaseTransport | None = None):
        self.base = base_url.rstrip("/")
        self.client = httpx.AsyncClient(base_url=self.base, timeout=httpx.Timeout(15.0), transport=transport)
        self.me_cache = _TTLCache(ttl)
        self.item_cache = _TTLCache(ttl)

    async def aclose(self):
        await self.client.aclose()

    async def me(self, token: str) -> dict:
        """Return the ABS user for this token or raise AbsError(401)."""
        k = token_key(token)
        hit = self.me_cache.get(k)
        if hit is not None:
            if isinstance(hit, AbsError):
                raise hit
            return hit
        try:
            r = await self.client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            raise AbsError(502, f"ABS unreachable: {e}") from e
        if r.status_code in (401, 403):
            err = AbsError(401, "invalid or expired token")
            self.me_cache.set(k, err)
            raise err
        if r.status_code >= 400:
            raise AbsError(502, f"ABS /api/me returned {r.status_code}")
        user = r.json()
        user = {"id": user.get("id"), "username": user.get("username"), "type": user.get("type")}
        self.me_cache.set(k, user)
        return user

    async def item(self, token: str, item_id: str) -> dict:
        """Fetch the item with the user's token. Raises AbsError 403/404."""
        k = (token_key(token), item_id)
        hit = self.item_cache.get(k)
        if hit is not None:
            if isinstance(hit, AbsError):
                raise hit
            return hit
        try:
            r = await self.client.get(f"/api/items/{item_id}", headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            raise AbsError(502, f"ABS unreachable: {e}") from e
        if r.status_code in (401,):
            raise AbsError(401, "invalid or expired token")
        if r.status_code in (403, 404):
            err = AbsError(r.status_code, "item not found or not accessible")
            self.item_cache.set(k, err)
            raise err
        if r.status_code >= 400:
            raise AbsError(502, f"ABS item lookup returned {r.status_code}")
        d = r.json()
        media = d.get("media") or {}
        info = {
            "id": d.get("id"),
            "duration": media.get("duration") or round(sum(
                float(f.get("duration") or 0) for f in media.get("audioFiles") or [] if not f.get("exclude")
            ), 3) or None,
            "hasAudio": bool(media.get("tracks") or media.get("audioFiles") or media.get("numTracks")),
            "hasEbook": bool(media.get("ebookFile") or media.get("ebookFormat")),
            "ebookFormat": ((media.get("ebookFile") or {}).get("ebookFormat") or media.get("ebookFormat") or ""),
            "ebookFile": ebook_file(d),
        }
        self.item_cache.set(k, info)
        return info

    async def download_ebook(self, token: str, item_id: str, max_bytes: int = 512 << 20) -> bytes:
        """The item's ebook file, read with the user's own token (so their access applies)."""
        try:
            async with self.client.stream("GET", f"/api/items/{item_id}/ebook",
                                          headers={"Authorization": f"Bearer {token}"},
                                          timeout=httpx.Timeout(120.0)) as r:
                if r.status_code == 401:
                    raise AbsError(401, "invalid or expired token")
                if r.status_code in (403, 404):
                    raise AbsError(r.status_code, "item not found or not accessible")
                if r.status_code >= 400:
                    raise AbsError(502, f"ABS ebook download returned {r.status_code}")
                out = bytearray()
                async for chunk in r.aiter_bytes():
                    out += chunk
                    if len(out) > max_bytes:
                        raise AbsError(413, "ebook file is too large")
                return bytes(out)
        except httpx.HTTPError as e:
            raise AbsError(502, f"ABS unreachable: {e}") from e
