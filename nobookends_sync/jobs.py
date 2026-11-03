"""Queueing policy shared by the API, admin endpoints and auto discovery."""

from __future__ import annotations

import time

from . import ALGO_VERSION
from .config import Settings
from .db import DB
from .results import delete_result

PRIORITY_AUTO = 0
PRIORITY_USER = 10
PRIORITY_ADMIN = 20


def _requeue(db: DB, job: dict, *, priority: int, source: str, max_duration, keep_result: bool):
    db.update_job(
        job["audio_id"], job["ebook_id"], status="queued", progress=0.0, priority=priority, source=source,
        max_duration=max_duration, keep_result=1 if keep_result else 0, error=None, started_at=None,
        finished_at=None,
    )


def request_alignment(
    db: DB,
    settings: Settings,
    audio_id: str,
    ebook_id: str,
    *,
    source: str,
    priority: int,
    sort_key: float | None = None,
    max_duration: float | None = None,
    force: bool = False,
) -> dict:
    """Idempotently make sure an alignment exists or is on its way.

    * never requested: queue it
    * queued: raise its priority if this request is more urgent
    * processing: leave it running; a more urgent request raises its priority
      (so an auto job a user now wants is not paused by the overnight window)
    * ready: re-run only when forced or produced by an older algorithm
      (the old result keeps being served meanwhile; transcripts are reused)
    * failed: re-run when forced, or on a user request after the retry window
    """
    job = db.get_job(audio_id, ebook_id)
    if job is None:
        db.insert_job(audio_id, ebook_id, priority=priority, source=source,
                      sort_key=sort_key if sort_key is not None else time.time(), max_duration=max_duration)
        return db.get_job(audio_id, ebook_id)
    st = job["status"]
    if st == "queued":
        if priority > job["priority"]:
            db.update_job(audio_id, ebook_id, priority=priority, source=source)
        if force:
            db.update_job(audio_id, ebook_id, max_duration=max_duration)
    elif st == "processing":
        if priority > job["priority"]:
            db.update_job(audio_id, ebook_id, priority=priority, source=source)
    elif st == "ready":
        if force or (job.get("result_algo") or 0) < ALGO_VERSION:
            _requeue(db, job, priority=priority, source=source, max_duration=max_duration, keep_result=True)
    elif st == "failed":
        retry_ok = priority >= PRIORITY_USER and (time.time() - (job.get("finished_at") or 0)) > settings.failed_retry_after_sec
        if force or retry_ok:
            _requeue(db, job, priority=priority, source=source, max_duration=max_duration, keep_result=False)
    return db.get_job(audio_id, ebook_id)


def requeue_changed(db: DB, settings: Settings, audio_id: str, ebook_id: str):
    """Source files changed: the old result no longer matches the audio/text.
    Re-done as automatic work (overnight); a reader who opens the book in the
    meantime raises it to user priority with their request."""
    job = db.get_job(audio_id, ebook_id)
    if not job:
        return
    delete_result(settings.results_dir, audio_id, ebook_id)
    _requeue(db, job, priority=PRIORITY_AUTO, source="auto-changed",
             max_duration=None, keep_result=False)


def requeue_outdated(db: DB, settings: Settings) -> int:
    n = 0
    for job in db.list_jobs(status="ready", limit=100000):
        if (job.get("result_algo") or 0) < ALGO_VERSION:
            _requeue(db, job, priority=PRIORITY_AUTO, source="auto-algo", max_duration=None, keep_result=True)
            n += 1
    return n
