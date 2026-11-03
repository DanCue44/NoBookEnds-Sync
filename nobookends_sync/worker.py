"""Single in-process job runner. Each job runs in a spawned child process.

Higher-priority work (an on-demand request) preempts a running auto job: the
child is stopped and the auto job goes back to the queue. Transcripts are
checkpointed per audio chunk, so the auto job resumes where it left off.

Automatic work (priority below user) only starts inside the AUTO_WINDOW
overnight window. An auto job still running when the window closes is asked to
pause at its next checkpoint; if it has not paused after
AUTO_PAUSE_GRACE_MINUTES it is stopped. Either way it resumes from its
transcript checkpoints when the window opens again. User and admin requests
are never gated.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import threading
import time

from .config import Settings
from .db import DB
from .jobs import PRIORITY_USER
from .pipeline import clear_pause, request_pause, run_job
from .schedule import window_open

log = logging.getLogger("nbs.worker")


class Worker(threading.Thread):
    def __init__(self, settings: Settings, db: DB, poll_sec: float = 2.0, target=run_job, is_window_open=None):
        super().__init__(name="nbs-worker", daemon=True)
        self.s = settings
        self.db = db
        self.poll_sec = poll_sec
        self.target = target
        self.window = settings.window
        self.is_window_open = is_window_open or (lambda: window_open(self.window))
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.current: tuple[str, str] | None = None
        self.pausing: tuple[str, str] | None = None
        self.last_beat = time.time()
        self._window_state: bool | None = None
        self._ctx = mp.get_context("spawn")

    def wake(self):
        self.wake_event.set()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()

    def auto_allowed(self) -> bool:
        is_open = bool(self.is_window_open())
        if is_open != self._window_state:
            log.info("auto window %s is %s", self.window or "always", "open" if is_open else "closed")
            self._window_state = is_open
        return is_open

    def next_job(self) -> dict | None:
        return self.db.next_queued(None if self.auto_allowed() else PRIORITY_USER)

    def run(self):
        self.db.reset_processing()
        clear_pause(self.db)
        while not self.stop_event.is_set():
            self.last_beat = time.time()
            job = self.next_job()
            if not job:
                self.wake_event.wait(self.poll_sec)
                self.wake_event.clear()
                continue
            self._run_one(job)

    def _run_one(self, job: dict):
        a, e = job["audio_id"], job["ebook_id"]
        priority = job["priority"]
        self.db.update_job(a, e, status="processing", started_at=time.time(), attempts=job["attempts"] + 1,
                           progress=job["progress"] or 0.0, error=None)
        self.current = (a, e)
        log.info("starting job %s / %s (priority %s, source %s)", a, e, job["priority"], job["source"])
        proc = self._ctx.Process(target=self.target, args=(self.s, a, e), name=f"nbs-job-{a[:8]}")
        proc.start()
        stopped = False
        pause_since: float | None = None
        try:
            while proc.is_alive():
                proc.join(self.poll_sec)
                self.last_beat = time.time()
                if not proc.is_alive():
                    break
                cur = self.db.get_job(a, e)
                if cur:
                    priority = cur["priority"]  # a user request for this very pair raises it
                if self.stop_event.is_set():
                    stopped = True
                else:
                    nxt = self.db.next_queued()
                    if nxt and nxt["priority"] > priority:
                        log.info("preempting %s / %s for higher priority %s / %s", a, e, nxt["audio_id"],
                                 nxt["ebook_id"])
                        stopped = True
                    elif priority >= PRIORITY_USER and pause_since is not None:
                        log.info("%s / %s was requested by a user; no longer pausing it", a, e)
                        clear_pause(self.db)
                        self.pausing = None
                        pause_since = None
                    elif priority < PRIORITY_USER and not self.auto_allowed():
                        if pause_since is None:
                            log.info("auto window closed: pausing %s / %s at its next checkpoint", a, e)
                            request_pause(self.db, a, e)
                            self.pausing = (a, e)
                            pause_since = time.time()
                        elif time.time() - pause_since > self.s.auto_pause_grace_minutes * 60:
                            log.info("auto job %s / %s did not reach a checkpoint in time; stopping it", a, e)
                            stopped = True
                if stopped:
                    proc.terminate()
                    proc.join(30)
                    if proc.is_alive():
                        proc.kill()
                        proc.join()
                    break
        finally:
            if pause_since is not None:
                clear_pause(self.db)
                self.pausing = None
            self.current = None
        after = self.db.get_job(a, e)
        if stopped:
            self.db.update_job(a, e, status="queued")
        elif after and after["status"] == "processing":
            self.db.update_job(a, e, status="failed", finished_at=time.time(),
                               error=f"worker process exited unexpectedly (code {proc.exitcode})")
