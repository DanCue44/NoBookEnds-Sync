"""Worker: one job at a time, on-demand work preempts auto jobs."""

import time

from nobookends_sync.config import Settings
from nobookends_sync.db import DB
from nobookends_sync.jobs import PRIORITY_ADMIN, PRIORITY_AUTO, PRIORITY_USER, request_alignment
from nobookends_sync.worker import Worker
from tests.worker_targets import checkpointing_job, fake_job


def _wait(pred, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def test_user_job_preempts_auto_job_which_then_resumes(tmp_path):
    s = Settings()
    s.data_dir = tmp_path
    s.auto_window = "always"
    s.ensure_dirs()
    db = DB(s.db_path)
    request_alignment(db, s, "slow-30", "e", source="auto", priority=PRIORITY_AUTO, sort_key=1)
    w = Worker(s, db, poll_sec=0.3, target=fake_job)
    w.start()
    try:
        assert _wait(lambda: db.get_job("slow-30", "e")["status"] == "processing")
        request_alignment(db, s, "quick", "e", source="user:alice", priority=PRIORITY_USER)
        assert _wait(lambda: db.get_job("quick", "e")["status"] == "ready")
        auto = db.get_job("slow-30", "e")
        assert auto["status"] in ("queued", "processing")
        assert auto["attempts"] >= 1
        # the auto job is picked up again afterwards
        assert _wait(lambda: db.get_job("slow-30", "e")["attempts"] >= 2)
    finally:
        w.stop()


def test_crashed_child_marks_job_failed(tmp_path):
    s = Settings()
    s.data_dir = tmp_path
    s.ensure_dirs()
    db = DB(s.db_path)
    request_alignment(db, s, "x", "e", source="user", priority=PRIORITY_USER)
    w = Worker(s, db, poll_sec=0.3, target=_not_importable_is_fine)
    w.start()
    try:
        assert _wait(lambda: db.get_job("x", "e")["status"] == "failed")
        assert "exited unexpectedly" in db.get_job("x", "e")["error"]
    finally:
        w.stop()


def _not_importable_is_fine(settings, a, e):
    raise SystemExit(3)


class Clock:
    """Switchable overnight window for tests."""

    def __init__(self, is_open):
        self.is_open = is_open

    def __call__(self):
        return self.is_open


def _env(tmp_path, grace_minutes=15.0):
    s = Settings()
    s.data_dir = tmp_path
    s.auto_window = "01:00-06:00"
    s.auto_pause_grace_minutes = grace_minutes
    s.ensure_dirs()
    return s, DB(s.db_path)


def test_auto_jobs_wait_for_window_but_user_jobs_do_not(tmp_path):
    s, db = _env(tmp_path)
    clock = Clock(False)
    request_alignment(db, s, "auto1", "e", source="auto", priority=PRIORITY_AUTO, sort_key=1)
    w = Worker(s, db, poll_sec=0.2, target=fake_job, is_window_open=clock)
    w.start()
    try:
        time.sleep(1.5)
        assert db.get_job("auto1", "e")["status"] == "queued"
        assert db.get_job("auto1", "e")["attempts"] == 0
        request_alignment(db, s, "user1", "e", source="user:alice", priority=PRIORITY_USER)
        assert _wait(lambda: db.get_job("user1", "e")["status"] == "ready")
        request_alignment(db, s, "admin1", "e", source="admin", priority=PRIORITY_ADMIN)
        assert _wait(lambda: db.get_job("admin1", "e")["status"] == "ready")
        time.sleep(0.8)
        assert db.get_job("auto1", "e")["status"] == "queued"
        clock.is_open = True
        assert _wait(lambda: db.get_job("auto1", "e")["status"] == "ready")
    finally:
        w.stop()


def test_running_auto_job_pauses_at_window_end_and_resumes(tmp_path):
    s, db = _env(tmp_path)
    clock = Clock(True)
    request_alignment(db, s, "ckpt-3", "e", source="auto", priority=PRIORITY_AUTO, sort_key=1)
    w = Worker(s, db, poll_sec=0.2, target=checkpointing_job, is_window_open=clock)
    w.start()
    try:
        assert _wait(lambda: db.kv_get("done:ckpt-3", 0) >= 0.5)
        clock.is_open = False
        assert _wait(lambda: db.kv_get("paused:ckpt-3", 0) == 1)
        assert _wait(lambda: db.get_job("ckpt-3", "e")["status"] == "queued")
        assert _wait(lambda: w.pausing is None and w.current is None and db.kv_get("worker.pause") is None)
        done_at_pause = db.kv_get("done:ckpt-3")
        time.sleep(1.0)
        job = db.get_job("ckpt-3", "e")
        assert job["status"] == "queued" and job["attempts"] == 1  # not restarted outside the window
        assert db.kv_get("done:ckpt-3") == done_at_pause
        clock.is_open = True
        assert _wait(lambda: db.get_job("ckpt-3", "e")["status"] == "ready")
        assert db.get_job("ckpt-3", "e")["attempts"] == 2
        assert db.kv_get("paused:ckpt-3") == 1
    finally:
        w.stop()


def test_auto_job_that_never_checkpoints_is_stopped_after_grace(tmp_path):
    s, db = _env(tmp_path, grace_minutes=0.01)  # 0.6 s
    clock = Clock(True)
    request_alignment(db, s, "slow-30", "e", source="auto", priority=PRIORITY_AUTO, sort_key=1)
    w = Worker(s, db, poll_sec=0.2, target=fake_job, is_window_open=clock)
    w.start()
    try:
        assert _wait(lambda: db.get_job("slow-30", "e")["status"] == "processing")
        clock.is_open = False
        assert _wait(lambda: db.get_job("slow-30", "e")["status"] == "queued", timeout=20)
        time.sleep(0.8)
        assert db.get_job("slow-30", "e")["attempts"] == 1
    finally:
        w.stop()


def test_user_request_for_the_running_auto_job_cancels_the_pause(tmp_path):
    s, db = _env(tmp_path, grace_minutes=0.02)
    clock = Clock(True)
    request_alignment(db, s, "slow-4", "e", source="auto", priority=PRIORITY_AUTO, sort_key=1)
    w = Worker(s, db, poll_sec=0.2, target=fake_job, is_window_open=clock)
    w.start()
    try:
        assert _wait(lambda: db.get_job("slow-4", "e")["status"] == "processing")
        request_alignment(db, s, "slow-4", "e", source="user:alice", priority=PRIORITY_USER)
        assert db.get_job("slow-4", "e")["priority"] == PRIORITY_USER
        clock.is_open = False
        assert _wait(lambda: db.get_job("slow-4", "e")["status"] == "ready")
        assert db.get_job("slow-4", "e")["attempts"] == 1
    finally:
        w.stop()
