"""Auto mode: discover audiobook/ebook pairs in ABS and queue alignments.

Every AUTO_POLL_MINUTES the service lists library items sorted by updatedAt
(newest first) and stops at the last watermark, so a poll only touches what
was added or changed. A full listing runs on first start (backfill) and every
AUTO_FULL_RESYNC_HOURS (to notice removed items).

Pairing works across libraries (audiobooks and ebooks often live in separate
libraries) and within one item that holds both audio and an EPUB:

1. same item with audio + EPUB
2. a shared ISBN (ISBN-10 normalized to ISBN-13) or ASIN
3. normalized title (no subtitle, no "(Unabridged)" and friends, no leading
   article, punctuation folded) + first author's surname

A finished pair is only re-queued when the audio or EPUB files change
(inode/size/mtime of every file, compared against the signature the last run
used).
"""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata

from .abs_client import AbsError, ServiceClient, file_signature
from .config import Settings
from .db import DB
from .jobs import PRIORITY_AUTO, request_alignment, requeue_changed, requeue_outdated

log = logging.getLogger("nbs.discovery")

_PAREN = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_EDITION_WORDS = re.compile(
    r"\b(unabridged|abridged|a novel|full cast( edition)?|dramati[sz]ed( adaptation)?|audiobook|audio edition|"
    r"illustrated edition|special edition|anniversary edition)\b"
)
_ARTICLES = re.compile(r"^(the|a|an)\s+")
_NONALNUM = re.compile(r"[^a-z0-9]+")


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().replace("&", " and ").replace("’", "'")


def normalize_title(title: str) -> str:
    t = _fold(title)
    t = _PAREN.sub(" ", t)
    t = re.split(r"\s*[:;]\s+|\s+[-\u2013\u2014]\s+", t)[0]  # drop subtitle
    t = _EDITION_WORDS.sub(" ", t)
    t = t.replace("'", "")
    t = _NONALNUM.sub(" ", t).strip()
    t = _ARTICLES.sub("", t)
    return t.strip()


def author_surname(author: str) -> str:
    a = _fold(author)
    first = re.split(r",|;| and | & |\s\+\s", a)[0].strip()
    first = re.sub(r"\b(jr|sr|ii|iii|iv|phd|md)\.?$", "", first).strip()
    parts = [p for p in _NONALNUM.sub(" ", first.replace("'", "")).split() if p]
    return parts[-1] if parts else ""


def _isbn13(raw: str) -> str | None:
    d = re.sub(r"[^0-9Xx]", "", raw or "").upper()
    if len(d) == 13 and d.isdigit():
        return d
    if len(d) == 10 and d[:9].isdigit():
        core = "978" + d[:9]
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
        return core + str((10 - total % 10) % 10)
    return None


def identifiers(md: dict) -> list[str]:
    out = []
    isbn = _isbn13(md.get("isbn") or "")
    if isbn:
        out.append("isbn:" + isbn)
    asin = (md.get("asin") or "").strip().upper()
    if re.fullmatch(r"[A-Z0-9]{10}", asin):
        out.append("asin:" + asin)
    return out


def item_record(item: dict, library_id: str | None = None) -> dict:
    """Catalog row from a (minified or expanded) ABS library item."""
    media = item.get("media") or {}
    md = media.get("metadata") or {}
    title = md.get("title") or ""
    author = md.get("authorName") or ", ".join(a.get("name", "") for a in md.get("authors") or [])
    ebook_format = (media.get("ebookFormat") or (media.get("ebookFile") or {}).get("ebookFormat") or "").lower()
    has_audio = bool(media.get("numTracks") or media.get("numAudioFiles") or media.get("tracks") or media.get("audioFiles"))
    key = normalize_title(title)
    sur = author_surname(author)
    return {
        "id": item["id"],
        "library_id": library_id or item.get("libraryId"),
        "title": title,
        "author": author,
        "match_key": f"{key}|{sur}" if key else None,
        "identifiers": identifiers(md),
        "has_audio": 1 if has_audio else 0,
        "has_ebook": 1 if ebook_format == "epub" else 0,
        "duration": media.get("duration"),
        "added_at": item.get("addedAt"),
        "updated_at": item.get("updatedAt"),
        "size": item.get("size"),
        "seen_at": time.time(),
    }


def find_pairs(items: list[dict], max_ebooks_per_audio: int = 3) -> list[tuple[str, str, str]]:
    """Return (audio_id, ebook_id, reason) for every audio item and its matching EPUB items."""
    ebooks = [i for i in items if i["has_ebook"] and not i.get("missing")]
    by_ident: dict[str, list[dict]] = {}
    by_key: dict[str, list[dict]] = {}
    for e in ebooks:
        for ident in e["identifiers"]:
            by_ident.setdefault(ident, []).append(e)
        if e["match_key"]:
            by_key.setdefault(e["match_key"], []).append(e)
    out = []
    for a in items:
        if not a["has_audio"] or a.get("missing"):
            continue
        if a["has_ebook"]:
            out.append((a["id"], a["id"], "same item"))
            continue
        found: dict[str, tuple[int, str, dict]] = {}
        for ident in a["identifiers"]:
            for e in by_ident.get(ident, []):
                found.setdefault(e["id"], (0, ident.split(":")[0], e))
        for e in by_key.get(a["match_key"] or "", []) if a["match_key"] else []:
            found.setdefault(e["id"], (1, "title+author", e))
        ranked = sorted(found.values(), key=lambda t: (t[0], -(t[2].get("updated_at") or 0)))
        for _, reason, e in ranked[:max_ebooks_per_audio]:
            if e["id"] != a["id"]:
                out.append((a["id"], e["id"], reason))
    return out


class Discovery(threading.Thread):
    def __init__(self, settings: Settings, db: DB, on_queued=None, client: ServiceClient | None = None):
        super().__init__(name="nbs-discovery", daemon=True)
        self.s = settings
        self.db = db
        self.client = client
        self.on_queued = on_queued or (lambda: None)
        self.stop_event = threading.Event()
        self.run_now = threading.Event()
        self.last_run: dict | None = None
        self.lock = threading.Lock()

    def stop(self):
        self.stop_event.set()
        self.run_now.set()

    def trigger(self):
        self.run_now.set()

    def run(self):
        try:
            n = requeue_outdated(self.db, self.s)
            if n:
                log.info("re-queued %d results from an older algorithm version", n)
                self.on_queued()
        except Exception:
            log.exception("requeue_outdated failed")
        while not self.stop_event.is_set():
            try:
                self.poll()
            except Exception as e:  # noqa: BLE001
                log.exception("discovery poll failed")
                self.last_run = {"at": time.time(), "error": str(e)[:300]}
            self.run_now.wait(self.s.auto_poll_minutes * 60)
            self.run_now.clear()

    # ------------------------------------------------------------------
    def poll(self, full: bool | None = None) -> dict:
        with self.lock:
            return self._poll(full)

    def _poll(self, full: bool | None) -> dict:
        client = self.client or ServiceClient(self.s.abs_url, self.s.abs_api_key)
        try:
            return self._poll_with(client, full)
        finally:
            if self.client is None:
                client.close()

    def _poll_with(self, client: ServiceClient, full: bool | None) -> dict:
        t0 = time.time()
        last_full = self.db.kv_get("discovery.last_full", 0)
        if full is None:
            full = (time.time() - last_full) > self.s.auto_full_resync_hours * 3600
        watermarks = self.db.kv_get("discovery.watermarks", {})
        seen: set[str] = set()
        changed: list[str] = []
        scanned = 0
        for lib in client.libraries():
            if lib.get("mediaType", "book") != "book":
                continue
            lid = lib["id"]
            wm = watermarks.get(lid, 0)
            newest = wm
            for it in client.iter_library_items(lid, sort="updatedAt", desc=True):
                scanned += 1
                upd = it.get("updatedAt") or 0
                newest = max(newest, upd)
                if not full and upd < wm:
                    break
                seen.add(it["id"])
                rec = item_record(it, lid)
                old = self.db.get_item(rec["id"])
                if old is None or old["updated_at"] != rec["updated_at"] or old["size"] != rec["size"] or old["missing"]:
                    changed.append(rec["id"])
                self.db.upsert_item(rec)
            watermarks[lid] = newest
        if full:
            self.db.mark_missing_except(seen)
            self.db.kv_set("discovery.last_full", time.time())
        self.db.kv_set("discovery.watermarks", watermarks)

        items = self.db.all_items()
        by_id = {i["id"]: i for i in items}
        queued = 0
        new_pairs = 0
        for a, e, reason in find_pairs(items, self.s.auto_max_ebooks_per_audio):
            if self.db.add_pair(a, e, reason):
                new_pairs += 1
            if self.db.get_job(a, e) is None:
                sort_key = max(by_id[a].get("added_at") or 0, by_id[e].get("added_at") or 0)
                request_alignment(self.db, self.s, a, e, source="auto", priority=PRIORITY_AUTO, sort_key=sort_key)
                queued += 1

        requeued = 0
        changed_set = set(changed)
        for pair in self.db.list_pairs():
            if pair["audio_id"] not in changed_set and pair["ebook_id"] not in changed_set:
                continue
            job = self.db.get_job(pair["audio_id"], pair["ebook_id"])
            if not job or job["status"] not in ("ready", "failed") or not job.get("signature"):
                continue
            try:
                ai = client.get_item(pair["audio_id"])
                ei = ai if pair["ebook_id"] == pair["audio_id"] else client.get_item(pair["ebook_id"])
            except AbsError as err:
                log.warning("could not re-check %s: %s", pair, err)
                continue
            if file_signature(ai, ei) != job["signature"]:
                log.info("files changed for %s / %s, re-queueing", pair["audio_id"], pair["ebook_id"])
                requeue_changed(self.db, self.s, pair["audio_id"], pair["ebook_id"])
                requeued += 1

        if queued or requeued:
            self.on_queued()
        self.last_run = {
            "at": time.time(), "full": full, "scanned": scanned, "changed": len(changed), "newPairs": new_pairs,
            "queued": queued, "requeuedChanged": requeued, "tookSec": round(time.time() - t0, 2),
        }
        log.info("discovery: %s", self.last_run)
        return self.last_run
