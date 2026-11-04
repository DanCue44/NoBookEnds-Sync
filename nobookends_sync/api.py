"""HTTP API (contract v1) plus admin endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import ALGO_VERSION, SCHEMA_VERSION, __version__
from .abs_client import AbsError, ServiceClient, UserAuth, audio_tracks, ebook_file
from .config import Settings, get_settings
from .converted import cache_key, cached_path, ensure_converted, is_kindle
from .db import DB
from .kindle import KINDLE_FORMATS, DrmProtected, KindleError
from .jobs import PRIORITY_ADMIN, PRIORITY_AUTO, PRIORITY_USER, request_alignment, requeue_outdated
from .results import load_result
from .schedule import parse_window, window_open
from .userdata import (
    COLLECTIONS, MAX_ITEMS_PER_POST, MAX_LIVE_ITEMS, PAGE_SIZE, TOMBSTONE_TTL_MS, BodyLimitMiddleware,
    validate_write,
)

log = logging.getLogger("nbs.api")


class AlignmentRequest(BaseModel):
    audioItemId: str = Field(min_length=1, max_length=200)
    ebookItemId: str = Field(min_length=1, max_length=200)


MAX_BULK_PAIRS = 500


class AdminAlignmentRequest(BaseModel):
    # one pair...
    audioItemId: str | None = Field(None, min_length=1, max_length=200)
    ebookItemId: str | None = Field(None, min_length=1, max_length=200)
    # ...or many (at most MAX_BULK_PAIRS)
    pairs: list[AlignmentRequest] | None = None
    # omitted: admin priority (runs first). "user": ahead of automatic work, not held for the
    # overnight window. "auto": automatic queue, only runs inside AUTO_WINDOW.
    priority: Literal["user", "auto"] | None = None
    # single pair only
    maxDurationSec: float | None = None
    force: bool = False
    # optional label shown in the admin job list as admin:<source>, e.g. "allseerr"
    source: str | None = Field(None, max_length=40, pattern=r"^[A-Za-z0-9_.-]*$")


_REASON_STATUS = {"not_found": 404, "no_audio": 422, "no_epub": 422, "abs_auth": 502, "abs_unavailable": 502}


def _reason(status: int, reason: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": message, "reason": reason}, status_code=status)


class UserDataWrite(BaseModel):
    # items are validated one by one (validate_write) so one bad item does not fail the batch
    items: list[Any]


TOMBSTONE_PURGE_EVERY_SEC = 6 * 3600


class RerunRequest(BaseModel):
    maxDurationSec: float | None = None


def detect_gpu(settings: Settings) -> bool:
    """Ask a short-lived subprocess so the API process never touches CUDA."""
    if settings.whisper_device == "cpu":
        return False
    try:
        out = subprocess.run(
            [sys.executable, "-c", "import ctranslate2; print(ctranslate2.get_cuda_device_count())"],
            capture_output=True, text=True, timeout=60,
        )
        return int(out.stdout.strip() or 0) > 0
    except Exception:
        return False


def _bearer(request: Request) -> str:
    h = request.headers.get("authorization") or ""
    if not h.lower().startswith("bearer ") or not h[7:].strip():
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    return h[7:].strip()


def _abs_http(e: AbsError) -> HTTPException:
    if e.status == 401:
        return HTTPException(401, "invalid or expired ABS token", headers={"WWW-Authenticate": "Bearer"})
    if e.status in (403, 404):
        return HTTPException(e.status, "item not found or not accessible")
    return HTTPException(502, "Audiobookshelf unavailable")


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    tags = [t.strip() for t in header.split(",")]
    return any(t == etag or t == "W/" + etag for t in tags)


def create_app(
    settings: Settings | None = None,
    *,
    user_transport=None,
    service_transport=None,
    start_background: bool = True,
    gpu: bool | None = None,
) -> FastAPI:
    s = settings or get_settings()
    try:
        parse_window(s.auto_window)
    except ValueError as e:
        raise SystemExit(f"configuration error: {e}") from e
    state: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s.ensure_dirs()
        db = DB(s.db_path)
        state["db"] = db
        state["auth"] = UserAuth(s.abs_url, ttl=s.auth_cache_sec, transport=user_transport)
        state["gpu"] = gpu if gpu is not None else detect_gpu(s)
        state["worker"] = None
        state["discovery"] = None
        purge_task = asyncio.create_task(purge_tombstones_loop(db))
        if start_background and s.alignment_enabled:
            import shutil

            shutil.rmtree(s.tmp_dir, ignore_errors=True)
            s.tmp_dir.mkdir(parents=True, exist_ok=True)
            from .worker import Worker

            worker = Worker(s, db)
            worker.start()
            state["worker"] = worker
            if s.auto_align and s.abs_api_key:
                from .discovery import Discovery

                disc = Discovery(s, db, on_queued=worker.wake)
                disc.start()
                state["discovery"] = disc
        log.info("NoBookEnds Sync %s up (gpu=%s, model=%s, alignment=%s)", __version__, state["gpu"],
                 s.whisper_model, s.alignment_enabled)
        yield
        purge_task.cancel()
        if state["discovery"]:
            state["discovery"].stop()
        if state["worker"]:
            state["worker"].stop()
        await state["auth"].aclose()

    app = FastAPI(title="NoBookEnds Sync", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(BodyLimitMiddleware)
    app.state.nbs = state

    async def purge_tombstones_loop(database: DB):
        while True:
            try:
                n = await asyncio.to_thread(database.ud_purge_tombstones, int(time.time() * 1000) - TOMBSTONE_TTL_MS)
                if n:
                    log.info("purged %d user data tombstones", n)
            except Exception:  # noqa: BLE001
                log.exception("tombstone purge failed")
            await asyncio.sleep(TOMBSTONE_PURGE_EVERY_SEC)

    def db() -> DB:
        return state["db"]

    async def check_access(request: Request, audio_id: str, ebook_id: str):
        token = _bearer(request)
        auth: UserAuth = state["auth"]
        try:
            user = await auth.me(token)
            audio = await auth.item(token, audio_id)
            ebook = audio if ebook_id == audio_id else await auth.item(token, ebook_id)
        except AbsError as e:
            raise _abs_http(e) from e
        return user, audio, ebook

    def status_object(audio_id: str, ebook_id: str, *, duration_fallback=None, with_segments=True,
                      start=None, end=None):
        """Build the contract status object. Returns (body, etag_seed)."""
        job = db().get_job(audio_id, ebook_id)
        body = {
            "status": "none",
            "progress": 0.0,
            "version": SCHEMA_VERSION,
            "audioItemId": audio_id,
            "ebookItemId": ebook_id,
            "durationSec": duration_fallback,
        }
        result = None
        if job:
            body["status"] = job["status"]
            body["progress"] = round(job["progress"] or 0.0, 4)
            if job.get("duration_sec"):
                body["durationSec"] = round(job["duration_sec"], 3)
            if job["status"] == "failed":
                body["error"] = job.get("error")
            if job["status"] == "queued":
                body["queuePosition"] = db().queue_position(audio_id, ebook_id)
            if job["status"] == "ready" or (job["keep_result"] and job["status"] in ("queued", "processing")):
                result = load_result(s.results_dir, audio_id, ebook_id)
                if result is not None:
                    if job["status"] != "ready":
                        body["updating"] = True
                        body["updateProgress"] = body["progress"]
                    body["status"] = "ready"
                    body["progress"] = 1.0
                    body["durationSec"] = result.doc.get("durationSec", body["durationSec"])
                    body["quality"] = result.doc.get("quality")
                    if result.doc.get("processedSec") and result.doc["processedSec"] < (body["durationSec"] or 0) - 1:
                        body["processedSec"] = result.doc["processedSec"]
                elif job["status"] == "ready":
                    body["status"] = "failed"
                    body["error"] = "result file missing"
        if with_segments:
            body["segments"] = result.window(start, end) if result is not None else []
        if result is not None:
            seed = f"{result.etag}|{body.get('updating', False)}|{start}|{end}"
        else:
            seed = json.dumps(body, sort_keys=True)
        etag = '"' + hashlib.sha256(seed.encode()).hexdigest()[:32] + '"'
        return body, etag

    # ------------------------------------------------------------------ public

    @app.get("/api/v1/health")
    async def health():
        w = state.get("worker")
        ok = True
        if start_background and s.alignment_enabled:
            ok = bool(w and w.is_alive())
        return {"ok": ok, "version": __version__, "gpu": bool(state.get("gpu"))}

    @app.get("/api/v1/capabilities")
    async def capabilities():
        return {"version": __version__, "alignment": bool(s.alignment_enabled), "userData": True,
                "collections": list(COLLECTIONS), "kindleEbooks": bool(s.kindle_ebooks),
                "kindleFormats": list(KINDLE_FORMATS) if s.kindle_ebooks else []}

    @app.get("/api/v1/alignments/{audio_item_id}")
    async def get_alignment(
        request: Request,
        audio_item_id: str,
        ebookItemId: str = Query(..., min_length=1),
        from_: float | None = Query(None, alias="from"),
        to: float | None = Query(None),
    ):
        _, audio, _ = await check_access(request, audio_item_id, ebookItemId)
        body, etag = status_object(audio_item_id, ebookItemId, duration_fallback=audio.get("duration"),
                                   start=from_, end=to)
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if _etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        return JSONResponse(body, headers=headers)

    @app.post("/api/v1/alignments")
    async def post_alignment(request: Request, req: AlignmentRequest):
        user, audio, ebook = await check_access(request, req.audioItemId, req.ebookItemId)
        if not s.alignment_enabled:
            return _reason(503, "alignment_disabled", "alignment is disabled on this server")
        if not audio.get("hasAudio"):
            raise HTTPException(400, "audioItemId has no audio")
        fmt = (ebook.get("ebookFormat") or "epub").lower()
        if not ebook.get("hasEbook") or (fmt != "epub" and not (s.kindle_ebooks and is_kindle(fmt))):
            raise HTTPException(400, "ebookItemId has no EPUB")
        request_alignment(db(), s, req.audioItemId, req.ebookItemId, source=f"user:{user.get('username')}",
                          priority=PRIORITY_USER)
        if state.get("worker"):
            state["worker"].wake()
        body, etag = status_object(req.audioItemId, req.ebookItemId, duration_fallback=audio.get("duration"),
                                   with_segments=False)
        return JSONResponse(body, headers={"ETag": etag, "Cache-Control": "no-store"})

    @app.get("/api/v1/ebooks/{ebook_item_id}/epub")
    async def kindle_epub(request: Request, ebook_item_id: str):
        """An AZW3 or MOBI ebook as the EPUB Listen & Read was aligned against."""
        token = _bearer(request)
        auth: UserAuth = state["auth"]
        try:
            await auth.me(token)
            item = await auth.item(token, ebook_item_id)
        except AbsError as e:
            raise _abs_http(e) from e
        eb = item.get("ebookFile")
        if not item.get("hasEbook") or not eb:
            return _reason(404, "no_ebook", "this item has no ebook file")
        if not is_kindle(eb.get("format")):
            return _reason(415, "not_kindle", "this ebook is not an AZW3 or MOBI file")
        if not s.kindle_ebooks:
            return _reason(503, "kindle_disabled", "Kindle ebooks are turned off on this server")
        etag = '"' + cache_key(ebook_item_id, eb) + '"'
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if _etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        try:
            path = cached_path(s.converted_dir, ebook_item_id, eb)
            if not path.exists():
                data = await auth.download_ebook(token, ebook_item_id)
                path = await asyncio.to_thread(ensure_converted, s.converted_dir, ebook_item_id, eb, lambda: data)
        except AbsError as e:
            if e.status == 413:
                return _reason(413, "too_large", "the ebook file is too large to convert")
            raise _abs_http(e) from e
        except DrmProtected:
            return _reason(422, "drm_protected", "this ebook is protected with DRM and cannot be opened")
        except KindleError as e:
            log.warning("could not convert ebook %s: %s", ebook_item_id, e)
            return _reason(422, "unreadable_ebook", "this ebook could not be converted")
        return FileResponse(path, media_type="application/epub+zip", headers=headers,
                            filename=f"{ebook_item_id}.epub")

    # ------------------------------------------------------------------ user data

    async def data_user(request: Request) -> str:
        """The caller's ABS user id. Tokens are never logged."""
        try:
            user = await state["auth"].me(_bearer(request))
        except AbsError as e:
            raise _abs_http(e) from e
        if not user.get("id"):
            raise HTTPException(502, "Audiobookshelf returned no user id")
        return str(user["id"])

    def check_collection(collection: str):
        if collection not in COLLECTIONS:
            raise HTTPException(404, "unknown collection")

    @app.get("/api/v1/me/data/{collection}")
    async def get_user_data(collection: str, user_id: str = Depends(data_user), since: int = Query(0, ge=0)):
        check_collection(collection)
        items, seq, more = await asyncio.to_thread(db().ud_changes, user_id, collection, since, PAGE_SIZE)
        return JSONResponse({"items": items, "seq": seq, "more": more}, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/me/data/{collection}")
    async def post_user_data(collection: str, body: UserDataWrite, user_id: str = Depends(data_user)):
        check_collection(collection)
        if len(body.items) > MAX_ITEMS_PER_POST:
            return _reason(413, "too_many_items", f"at most {MAX_ITEMS_PER_POST} items per request")
        writes, rejected = [], []
        for raw in body.items:
            w, problem = validate_write(raw)
            if problem:
                rejected.append(problem)
            else:
                writes.append(w)
        applied, refused, seq = await asyncio.to_thread(db().ud_apply, user_id, collection, writes, MAX_LIVE_ITEMS)
        return JSONResponse({"applied": applied, "rejected": rejected + refused, "seq": seq},
                            headers={"Cache-Control": "no-store"})

    @app.delete("/api/v1/me/data")
    async def wipe_user_data(user_id: str = Depends(data_user)):
        n = await asyncio.to_thread(db().ud_wipe, user_id)
        return {"deleted": n}

    # ------------------------------------------------------------------ admin

    def require_admin(request: Request):
        if not s.admin_token:
            raise HTTPException(404, "not found")
        tok = _bearer(request)
        if not hmac.compare_digest(tok.encode(), s.admin_token.encode()):
            raise HTTPException(403, "forbidden")

    def auto_window_open() -> bool:
        return window_open(s.window)

    def job_view(j: dict) -> dict:
        items = {}
        for k in ("audio_id", "ebook_id"):
            it = db().get_item(j[k])
            if it:
                items[k] = {"title": it["title"], "author": it["author"]}
        return {
            "audioItemId": j["audio_id"], "ebookItemId": j["ebook_id"], "status": j["status"],
            "progress": j["progress"], "priority": j["priority"], "source": j["source"],
            "maxDurationSec": j["max_duration"], "createdAt": j["created_at"], "updatedAt": j["updated_at"],
            "startedAt": j["started_at"], "finishedAt": j["finished_at"], "error": j["error"],
            "durationSec": j["duration_sec"], "quality": j["quality"], "stats": j["stats"],
            "resultAlgoVersion": j["result_algo"], "attempts": j["attempts"], "keepResult": j["keep_result"],
            "waitingForWindow": j["status"] == "queued" and j["priority"] < PRIORITY_USER and not auto_window_open(),
            "titles": items,
        }

    @app.get("/api/v1/admin/jobs", dependencies=[Depends(require_admin)])
    async def admin_jobs(status: str | None = None, limit: int = 500):
        jobs = db().list_jobs(status=status, limit=limit)
        w = state.get("worker")
        d = state.get("discovery")
        return {
            "version": __version__, "algoVersion": ALGO_VERSION,
            "settings": {
                "model": s.whisper_model, "device": s.whisper_device, "computeType": s.whisper_compute_type,
                "beamSize": s.whisper_beam_size, "cpuThreads": s.cpu_threads, "vad": s.whisper_vad,
                "maxAudioSec": s.max_audio_sec, "autoAlign": s.auto_align, "autoPollMinutes": s.auto_poll_minutes,
                "autoWindow": str(s.window) if s.window else "always", "autoWindowOpen": auto_window_open(),
                "autoPauseGraceMinutes": s.auto_pause_grace_minutes,
            },
            "worker": {
                "alive": bool(w and w.is_alive()),
                "current": list(w.current) if w and w.current else None,
                "pausing": list(w.pausing) if w and w.pausing else None,
                "lastBeat": w.last_beat if w else None,
            },
            "discovery": {"enabled": bool(d), "lastRun": d.last_run if d else None},
            "counts": {st: sum(1 for j in jobs if j["status"] == st) for st in ("queued", "processing", "ready", "failed")},
            "jobs": [job_view(j) for j in jobs],
        }

    @app.get("/api/v1/admin/jobs/{audio_id}/{ebook_id}", dependencies=[Depends(require_admin)])
    async def admin_job(audio_id: str, ebook_id: str):
        j = db().get_job(audio_id, ebook_id)
        if not j:
            raise HTTPException(404, "no such job")
        return job_view(j)

    @app.post("/api/v1/admin/jobs/{audio_id}/{ebook_id}/rerun", dependencies=[Depends(require_admin)])
    async def admin_rerun(audio_id: str, ebook_id: str, req: RerunRequest | None = None):
        if not db().get_job(audio_id, ebook_id):
            raise HTTPException(404, "no such job")
        j = request_alignment(db(), s, audio_id, ebook_id, source="admin", priority=PRIORITY_ADMIN,
                              max_duration=req.maxDurationSec if req else None, force=True)
        if state.get("worker"):
            state["worker"].wake()
        return job_view(j)

    @app.post("/api/v1/admin/alignments", dependencies=[Depends(require_admin)])
    async def admin_queue(req: AdminAlignmentRequest):
        """Queue one pair ({audioItemId, ebookItemId}) or many ({pairs: [...]}).

        Idempotent. Without `priority` jobs run at admin priority (the original
        behavior); "user" runs ahead of automatic work and ignores the overnight
        window; "auto" joins the automatic queue and only runs inside AUTO_WINDOW.
        Pairs are checked against ABS with the service key first: the audio item
        must have audio and the ebook item an EPUB (422 with a reason otherwise)."""
        single = req.pairs is None
        if single and not (req.audioItemId and req.ebookItemId):
            return _reason(422, "missing_ids", "send audioItemId and ebookItemId, or pairs")
        if not single and (req.audioItemId or req.ebookItemId):
            return _reason(422, "ambiguous", "send either audioItemId/ebookItemId or pairs, not both")
        if not single and len(req.pairs) > MAX_BULK_PAIRS:
            return _reason(422, "too_many_pairs", f"at most {MAX_BULK_PAIRS} pairs per call")
        priority = {None: PRIORITY_ADMIN, "user": PRIORITY_USER, "auto": PRIORITY_AUTO}[req.priority]
        source = f"admin:{req.source}" if req.source else "admin"
        pairs = [(req.audioItemId, req.ebookItemId)] if single else [(p.audioItemId, p.ebookItemId) for p in req.pairs]

        def work():
            checks = validate_pairs(pairs) if s.abs_api_key else [None] * len(pairs)
            out = []
            for (a, e), problem in zip(pairs, checks):
                if problem:
                    out.append((a, e, problem, None))
                    continue
                j = request_alignment(db(), s, a, e, source=source, priority=priority,
                                      max_duration=req.maxDurationSec if single else None,
                                      force=req.force if single else False)
                out.append((a, e, None, j))
            return out

        results = await asyncio.to_thread(work)
        if state.get("worker") and any(j for *_, j in results):
            state["worker"].wake()
        if single:
            _, _, problem, j = results[0]
            if problem:
                return _reason(_REASON_STATUS[problem[0]], *problem)
            return job_view(j)
        items = []
        for a, e, problem, j in results:
            if problem:
                items.append({"audioItemId": a, "ebookItemId": e, "ok": False, "reason": problem[0],
                              "error": problem[1]})
            else:
                v = job_view(j)
                items.append({"audioItemId": a, "ebookItemId": e, "ok": True, "status": v["status"],
                              "priority": v["priority"], "waitingForWindow": v["waitingForWindow"]})
        ok = sum(1 for it in items if it["ok"])
        return {"accepted": ok, "rejected": len(items) - ok, "results": items}

    def validate_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str] | None]:
        """For each pair None when it can be aligned, else (reason, message).
        Items are fetched once per call with the service key, a few at a time."""
        from concurrent.futures import ThreadPoolExecutor

        client = ServiceClient(s.abs_url, s.abs_api_key, transport=service_transport)
        cache: dict[str, tuple[str, dict | None]] = {}

        def fetch(item_id: str):
            try:
                cache[item_id] = ("ok", client.get_item(item_id, expanded=True))
            except AbsError as err:
                if err.status in (403, 404):
                    cache[item_id] = ("not_found", None)
                elif err.status == 401:
                    cache[item_id] = ("abs_auth", None)
                else:
                    cache[item_id] = ("abs_unavailable", None)
            except Exception:  # noqa: BLE001 (connection errors)
                cache[item_id] = ("abs_unavailable", None)

        def check(a: str, e: str):
            for item_id, label in ((a, "audioItemId"), (e, "ebookItemId")):
                st, _ = cache[item_id]
                if st == "not_found":
                    return ("not_found", f"{label} not found in Audiobookshelf")
                if st == "abs_auth":
                    return ("abs_auth", "Audiobookshelf rejected the service API key")
                if st != "ok":
                    return ("abs_unavailable", "Audiobookshelf unavailable")
            if not audio_tracks(cache[a][1]):
                return ("no_audio", "audioItemId has no audio")
            eb = ebook_file(cache[e][1])
            if not eb or eb["format"] != "epub":
                return ("no_epub", "ebookItemId has no EPUB")
            return None

        try:
            ids = list(dict.fromkeys(i for pair in pairs for i in pair))
            with ThreadPoolExecutor(max_workers=min(8, len(ids) or 1)) as pool:
                list(pool.map(fetch, ids))
            return [check(a, e) for a, e in pairs]
        finally:
            client.close()

    @app.get("/api/v1/admin/status", dependencies=[Depends(require_admin)])
    async def admin_status():
        """Cheap summary for dashboards (AllSeerr Settings > Services)."""
        w = state.get("worker")
        win = s.window
        counts = db().queue_counts(PRIORITY_USER)
        current = None
        if w and w.current:
            a, e = w.current
            j = db().get_job(a, e) or {}
            it = db().get_item(a) or {}
            current = {"audioItemId": a, "ebookItemId": e, "title": it.get("title"),
                       "progress": round(j.get("progress") or 0.0, 4)}
        return {
            "version": __version__,
            "gpu": bool(state.get("gpu")),
            "window": {
                "start": f"{win.start:%H:%M}" if win else None,
                "end": f"{win.end:%H:%M}" if win else None,
                "active": auto_window_open(),
            },
            "queue": {"user": counts["user"], "auto": counts["auto"], "total": counts["user"] + counts["auto"]},
            "current": current,
            "userData": userdata_status(),
        }

    def userdata_status() -> dict:
        st = db().ud_stats()
        return {"users": st["users"], "collections": {
            c: st["collections"].get(c, {"items": 0, "deleted": 0, "users": 0}) for c in COLLECTIONS}}

    @app.get("/api/v1/admin/pairs", dependencies=[Depends(require_admin)])
    async def admin_pairs():
        out = []
        for p in db().list_pairs():
            j = db().get_job(p["audio_id"], p["ebook_id"])
            a = db().get_item(p["audio_id"]) or {}
            e = db().get_item(p["ebook_id"]) or {}
            out.append({
                "audioItemId": p["audio_id"], "ebookItemId": p["ebook_id"], "reason": p["reason"],
                "discoveredAt": p["discovered_at"], "title": a.get("title"), "author": a.get("author"),
                "ebookTitle": e.get("title"), "status": j["status"] if j else "none",
                "progress": j["progress"] if j else 0,
            })
        return {"pairs": out, "items": len(db().all_items())}

    @app.post("/api/v1/admin/discovery/run", dependencies=[Depends(require_admin)])
    async def admin_discovery_run(full: bool = False):
        d = state.get("discovery")
        if not d:
            raise HTTPException(409, "auto mode is disabled")

        res = await asyncio.to_thread(d.poll, True if full else None)
        return res

    @app.post("/api/v1/admin/realign-outdated", dependencies=[Depends(require_admin)])
    async def admin_realign_outdated():
        n = requeue_outdated(db(), s)
        if state.get("worker"):
            state["worker"].wake()
        return {"requeued": n}

    return app
