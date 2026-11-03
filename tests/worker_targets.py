"""Job targets for the worker test (must be importable by spawned processes)."""

import time

from nobookends_sync.db import DB


def fake_job(settings, audio_id, ebook_id):
    db = DB(settings.db_path)
    if audio_id.startswith("slow"):
        db.update_job(audio_id, ebook_id, progress=0.1)
        time.sleep(float(audio_id.split("-")[1]))
    db.update_job(audio_id, ebook_id, status="ready", progress=1.0)


def checkpointing_job(settings, audio_id, ebook_id):
    """Works for N seconds (audio_id "ckpt-N"), checkpointing every 0.1 s and
    honoring pause requests at checkpoints like the real pipeline."""
    from nobookends_sync.pipeline import mark_paused, pause_requested

    db = DB(settings.db_path)
    total = float(audio_id.split("-")[1])
    done = db.kv_get(f"done:{audio_id}", 0.0)
    while done < total:
        time.sleep(0.1)
        done = round(done + 0.1, 3)
        db.kv_set(f"done:{audio_id}", done)
        if pause_requested(db, audio_id, ebook_id):
            db.kv_set(f"paused:{audio_id}", db.kv_get(f"paused:{audio_id}", 0) + 1)
            mark_paused(db, audio_id, ebook_id)
            return
    db.update_job(audio_id, ebook_id, status="ready", progress=1.0)
