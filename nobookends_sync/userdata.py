"""User data sync: per-user collections the app keeps on the server (contract v1, 2026-10-14).

Audiobookshelf has no place for podcast subscriptions made in the app, their
progress, or ratings, highlights, clips and the last reading mode per book synced
across one user's devices.
These live here, keyed by the ABS user id. Nothing in this module touches the
alignment worker or the GPU.
"""

from __future__ import annotations

import json
import math

from starlette.exceptions import HTTPException

# readingState (1.3.0): the app's last mode per book (listen, read, Listen & Read) for Continue.
# achievements (1.5.0): earned Shelf badges and celebrated milestones.
# listeningPaths (1.6.0): the chosen path or custom order per multi-path audiobook.
# appSettings (1.4.0): the app's own settings, one record per account, for settings sync between devices.
COLLECTIONS = (
    "podcastSubscriptions", "podcastProgress", "ratings", "highlights", "clips", "readingState", "appSettings",
    "achievements", "listeningPaths",
)

MAX_ITEMS_PER_POST = 500
MAX_ITEM_BYTES = 64 * 1024          # serialized `data` per item
MAX_LIVE_ITEMS = 50_000             # per user per collection
MAX_ID_LEN = 200
PAGE_SIZE = 1000
TOMBSTONE_TTL_MS = 90 * 86400 * 1000
MAX_BODY_BYTES = 8 * 1024 * 1024    # whole request body, every endpoint


def validate_write(raw) -> tuple[dict | None, dict | None]:
    """One posted item -> (write, None) or (None, {id, reason}).

    Reasons: "invalid" (bad id, data or updatedAt), "too_large" (data over
    MAX_ITEM_BYTES). `data` is ignored for deletes."""
    if not isinstance(raw, dict):
        return None, {"id": None, "reason": "invalid"}
    item_id = raw.get("id")
    if not isinstance(item_id, str) or not item_id or len(item_id) > MAX_ID_LEN:
        return None, {"id": item_id if isinstance(item_id, str) else None, "reason": "invalid"}
    bad = {"id": item_id, "reason": "invalid"}
    ts = raw.get("updatedAt")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None, bad
    if isinstance(ts, float):
        if not math.isfinite(ts) or ts != int(ts):
            return None, bad
        ts = int(ts)
    if ts < 0 or ts > 2**53:
        return None, bad
    deleted = raw.get("deleted", False)
    if deleted is None:
        deleted = False
    if not isinstance(deleted, bool):
        return None, bad
    data = None
    if not deleted:
        if not isinstance(raw.get("data"), dict):
            return None, bad
        data = json.dumps(raw["data"], separators=(",", ":"), ensure_ascii=False)
        if len(data.encode("utf-8")) > MAX_ITEM_BYTES:
            return None, {"id": item_id, "reason": "too_large"}
    return {"id": item_id, "data": data, "updated_at": ts, "deleted": deleted}, None


class _TooLarge(HTTPException):
    """Raised from receive() while the app reads the body; an HTTPException so FastAPI's body
    parsing passes it through as a 413 instead of a parse error."""

    def __init__(self):
        super().__init__(413, "request body too large")


class BodyLimitMiddleware:
    """Reject request bodies over max_bytes with 413, by Content-Length up front and by
    counting while the body streams in (chunked uploads)."""

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max = max_bytes

    async def _reject(self, send):
        body = json.dumps({"detail": "request body too large", "reason": "body_too_large"}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for k, v in scope.get("headers") or []:
            if k == b"content-length":
                try:
                    if int(v) > self.max:
                        return await self._reject(send)
                except ValueError:
                    pass
        seen = 0
        started = False

        async def limited_receive():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body") or b"")
                if seen > self.max:
                    raise _TooLarge()
            return msg

        async def tracking_send(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            if not started:
                await self._reject(send)
