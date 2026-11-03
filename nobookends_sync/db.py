"""SQLite persistence: jobs, the ABS item catalog, discovered pairs, kv, user data."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    audio_id TEXT NOT NULL,
    ebook_id TEXT NOT NULL,
    status TEXT NOT NULL,              -- queued | processing | ready | failed
    progress REAL NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,  -- 0 auto, 10 user, 20 admin
    sort_key REAL NOT NULL DEFAULT 0,  -- lower runs first within a priority
    source TEXT NOT NULL DEFAULT 'auto',
    max_duration REAL,
    keep_result INTEGER NOT NULL DEFAULT 0,  -- serve the previous result while re-running
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    error TEXT,
    duration_sec REAL,
    quality TEXT,
    stats TEXT,
    signature TEXT,                    -- file signature the result was built from
    result_algo INTEGER,
    result_etag TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (audio_id, ebook_id)
);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    library_id TEXT,
    title TEXT,
    author TEXT,
    match_key TEXT,
    identifiers TEXT,                  -- JSON list
    has_audio INTEGER NOT NULL DEFAULT 0,
    has_ebook INTEGER NOT NULL DEFAULT 0,
    duration REAL,
    added_at REAL,
    updated_at REAL,
    size INTEGER,
    seen_at REAL,
    missing INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_key ON items(match_key);
CREATE TABLE IF NOT EXISTS pairs (
    audio_id TEXT NOT NULL,
    ebook_id TEXT NOT NULL,
    reason TEXT,
    discovered_at REAL,
    PRIMARY KEY (audio_id, ebook_id)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
-- user data sync (1.2.0): one row per (ABS user, collection, client id); deletes are tombstones
CREATE TABLE IF NOT EXISTS user_data (
    user_id TEXT NOT NULL,
    collection TEXT NOT NULL,
    id TEXT NOT NULL,
    seq INTEGER NOT NULL,              -- per-user change counter, see user_seq
    updated_at INTEGER NOT NULL,       -- client updatedAt, ms epoch (last write wins)
    deleted INTEGER NOT NULL DEFAULT 0,
    data TEXT,                         -- JSON object, NULL for tombstones
    stored_at INTEGER NOT NULL,        -- server time of the write, ms epoch (tombstone purge)
    PRIMARY KEY (user_id, collection, id)
);
CREATE INDEX IF NOT EXISTS user_data_seq ON user_data(user_id, collection, seq);
CREATE INDEX IF NOT EXISTS user_data_tombstones ON user_data(deleted, stored_at);
CREATE TABLE IF NOT EXISTS user_seq (user_id TEXT PRIMARY KEY, seq INTEGER NOT NULL);
"""


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        try:
            yield c
        finally:
            c.close()

    # ------------------------------------------------------------ kv
    def kv_get(self, key, default=None):
        with self.conn() as c:
            r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def kv_set(self, key, value):
        with self.conn() as c:
            c.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value)))

    # ------------------------------------------------------------ jobs
    def get_job(self, audio_id, ebook_id) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE audio_id=? AND ebook_id=?", (audio_id, ebook_id)).fetchone()
        return _job(r)

    def list_jobs(self, status: str | None = None, limit: int = 500) -> list[dict]:
        q = "SELECT * FROM jobs"
        args: tuple = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        q += " ORDER BY CASE status WHEN 'processing' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, priority DESC, sort_key ASC, updated_at DESC LIMIT ?"
        with self.conn() as c:
            return [_job(r) for r in c.execute(q, args + (limit,)).fetchall()]

    def insert_job(self, audio_id, ebook_id, *, priority, source, sort_key, max_duration=None):
        now = time.time()
        with self.conn() as c:
            c.execute(
                "INSERT INTO jobs(audio_id,ebook_id,status,progress,priority,sort_key,source,max_duration,created_at,updated_at)"
                " VALUES(?,?,'queued',0,?,?,?,?,?,?)",
                (audio_id, ebook_id, priority, sort_key, source, max_duration, now, now),
            )

    def update_job(self, audio_id, ebook_id, **fields):
        if not fields:
            return
        fields["updated_at"] = time.time()
        for k in ("quality", "stats"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE audio_id=? AND ebook_id=?",
                      tuple(fields.values()) + (audio_id, ebook_id))

    def next_queued(self, min_priority: int | None = None) -> dict | None:
        """The job to run next; with min_priority, only jobs at or above it."""
        q = "SELECT * FROM jobs WHERE status='queued'"
        args: tuple = ()
        if min_priority is not None:
            q += " AND priority>=?"
            args = (min_priority,)
        q += " ORDER BY priority DESC, sort_key ASC, created_at ASC LIMIT 1"
        with self.conn() as c:
            r = c.execute(q, args).fetchone()
        return _job(r)

    def queue_counts(self, user_priority: int) -> dict:
        """Queued jobs split into user/admin work and automatic work."""
        with self.conn() as c:
            r = c.execute(
                "SELECT SUM(priority>=?) AS user, SUM(priority<?) AS auto FROM jobs WHERE status='queued'",
                (user_priority, user_priority),
            ).fetchone()
        return {"user": int(r["user"] or 0), "auto": int(r["auto"] or 0)}

    def reset_processing(self):
        """After a restart, interrupted jobs go back to the queue."""
        with self.conn() as c:
            c.execute("UPDATE jobs SET status='queued' WHERE status='processing'")

    def queue_position(self, audio_id, ebook_id) -> int | None:
        j = self.get_job(audio_id, ebook_id)
        if not j or j["status"] != "queued":
            return None
        with self.conn() as c:
            r = c.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status='queued' AND (priority>? OR (priority=? AND"
                " (sort_key<? OR (sort_key=? AND created_at<?))))",
                (j["priority"], j["priority"], j["sort_key"], j["sort_key"], j["created_at"]),
            ).fetchone()
        return int(r["n"])

    # ------------------------------------------------------------ items / pairs
    def upsert_item(self, it: dict):
        cols = ["id", "library_id", "title", "author", "match_key", "identifiers", "has_audio", "has_ebook",
                "duration", "added_at", "updated_at", "size", "seen_at", "missing"]
        vals = [it.get(k) for k in cols]
        vals[cols.index("identifiers")] = json.dumps(it.get("identifiers") or [])
        vals[cols.index("missing")] = 0
        with self.conn() as c:
            c.execute(
                f"INSERT INTO items({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
                " ON CONFLICT(id) DO UPDATE SET " + ", ".join(f"{k}=excluded.{k}" for k in cols[1:]),
                vals,
            )

    def get_item(self, item_id) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return _item(r)

    def all_items(self, include_missing=False) -> list[dict]:
        q = "SELECT * FROM items" + ("" if include_missing else " WHERE missing=0")
        with self.conn() as c:
            return [_item(r) for r in c.execute(q).fetchall()]

    def mark_missing_except(self, seen_ids: set[str]):
        with self.conn() as c:
            for r in c.execute("SELECT id FROM items WHERE missing=0").fetchall():
                if r["id"] not in seen_ids:
                    c.execute("UPDATE items SET missing=1 WHERE id=?", (r["id"],))

    def add_pair(self, audio_id, ebook_id, reason) -> bool:
        with self.conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO pairs(audio_id,ebook_id,reason,discovered_at) VALUES(?,?,?,?)",
                (audio_id, ebook_id, reason, time.time()),
            )
            return cur.rowcount > 0

    def list_pairs(self) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM pairs ORDER BY discovered_at").fetchall()]

    def pairs_for_item(self, item_id) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM pairs WHERE audio_id=? OR ebook_id=?", (item_id, item_id)).fetchall()]

    # ------------------------------------------------------------ user data
    def ud_apply(self, user_id: str, collection: str, writes: list[dict], max_live: int) -> tuple[list, list, int]:
        """Apply validated writes ({id, data (JSON text or None), updated_at, deleted}) in order,
        in one transaction. Last write wins by updated_at; a tie keeps the stored row.
        Returns (applied ids, rejected [{id, reason}], latest seq for user+collection)."""
        applied: list[str] = []
        rejected: list[dict] = []
        now = int(time.time() * 1000)
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                r = c.execute("SELECT seq FROM user_seq WHERE user_id=?", (user_id,)).fetchone()
                seq = start_seq = int(r["seq"]) if r else 0
                live = c.execute("SELECT COUNT(*) AS n FROM user_data WHERE user_id=? AND collection=? AND deleted=0",
                                 (user_id, collection)).fetchone()["n"]
                for w in writes:
                    row = c.execute(
                        "SELECT updated_at, deleted FROM user_data WHERE user_id=? AND collection=? AND id=?",
                        (user_id, collection, w["id"])).fetchone()
                    if row is not None and w["updated_at"] <= row["updated_at"]:
                        rejected.append({"id": w["id"], "reason": "stale"})
                        continue
                    was_live = row is not None and not row["deleted"]
                    if not w["deleted"] and not was_live and live >= max_live:
                        rejected.append({"id": w["id"], "reason": "quota"})
                        continue
                    seq += 1
                    c.execute(
                        "INSERT INTO user_data(user_id,collection,id,seq,updated_at,deleted,data,stored_at)"
                        " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,collection,id) DO UPDATE SET"
                        " seq=excluded.seq, updated_at=excluded.updated_at, deleted=excluded.deleted,"
                        " data=excluded.data, stored_at=excluded.stored_at",
                        (user_id, collection, w["id"], seq, w["updated_at"], int(w["deleted"]),
                         None if w["deleted"] else w["data"], now),
                    )
                    live += (0 if w["deleted"] else 1) - (1 if was_live else 0)
                    applied.append(w["id"])
                if seq != start_seq:
                    c.execute("INSERT INTO user_seq(user_id,seq) VALUES(?,?)"
                              " ON CONFLICT(user_id) DO UPDATE SET seq=excluded.seq", (user_id, seq))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            latest = self._ud_latest(c, user_id, collection)
        return applied, rejected, latest

    @staticmethod
    def _ud_latest(c, user_id: str, collection: str) -> int:
        r = c.execute("SELECT MAX(seq) AS s FROM user_data WHERE user_id=? AND collection=?",
                      (user_id, collection)).fetchone()
        return int(r["s"] or 0)

    def ud_changes(self, user_id: str, collection: str, since: int, limit: int) -> tuple[list[dict], int, bool]:
        """Rows with seq > since, oldest change first. Returns (items, seq, more): with more,
        seq is the last returned item's seq (ask again with it), else the latest seq."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT id, data, updated_at, deleted, seq FROM user_data WHERE user_id=? AND collection=? AND seq>?"
                " ORDER BY seq LIMIT ?", (user_id, collection, since, limit + 1)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            seq = rows[-1]["seq"] if more else max(since, self._ud_latest(c, user_id, collection))
        items = [{"id": r["id"], "data": json.loads(r["data"]) if r["data"] is not None else None,
                  "updatedAt": r["updated_at"], "deleted": bool(r["deleted"]), "seq": r["seq"]} for r in rows]
        return items, int(seq), more

    def ud_wipe(self, user_id: str) -> int:
        """Delete every row of this user in every collection (tombstones included)."""
        with self.conn() as c:
            return c.execute("DELETE FROM user_data WHERE user_id=?", (user_id,)).rowcount

    def ud_purge_tombstones(self, older_than_ms: int) -> int:
        with self.conn() as c:
            return c.execute("DELETE FROM user_data WHERE deleted=1 AND stored_at<?", (older_than_ms,)).rowcount

    def ud_stats(self) -> dict:
        """Per collection: live items, tombstones and users with live items."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT collection, SUM(deleted=0) AS items, SUM(deleted=1) AS deleted,"
                " COUNT(DISTINCT CASE WHEN deleted=0 THEN user_id END) AS users FROM user_data GROUP BY collection"
            ).fetchall()
            users = c.execute("SELECT COUNT(DISTINCT user_id) AS n FROM user_data WHERE deleted=0").fetchone()["n"]
        return {"users": int(users), "collections": {
            r["collection"]: {"items": int(r["items"] or 0), "deleted": int(r["deleted"] or 0),
                              "users": int(r["users"] or 0)} for r in rows}}


def _job(r):
    if r is None:
        return None
    d = dict(r)
    for k in ("quality", "stats"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    d["keep_result"] = bool(d["keep_result"])
    return d


def _item(r):
    if r is None:
        return None
    d = dict(r)
    d["identifiers"] = json.loads(d["identifiers"] or "[]")
    return d
