"""Auto mode: pairing rules, backfill order, change detection."""

import httpx
import pytest

from nobookends_sync import ALGO_VERSION
from nobookends_sync.abs_client import ServiceClient
from nobookends_sync.config import Settings
from nobookends_sync.db import DB
from nobookends_sync.discovery import (
    Discovery,
    author_surname,
    find_pairs,
    item_record,
    normalize_title,
)


def test_normalize_title():
    assert normalize_title("The Poppy War") == "poppy war"
    assert normalize_title("Steve Jobs (Unabridged)") == "steve jobs"
    assert normalize_title("Caught Up: Into Darkness, Book 2") == "caught up"
    assert normalize_title("A Game of Thrones - A Song of Ice and Fire, Book 1") == "game of thrones"
    assert normalize_title("Ender’s Game [Dramatized Adaptation]") == "enders game"
    assert normalize_title("Café Society") == "cafe society"
    assert normalize_title("1984") == "1984"


def test_author_surname():
    assert author_surname("Walter Isaacson") == "isaacson"
    assert author_surname("R.F. Kuang") == "kuang"
    assert author_surname("Neil Gaiman, Terry Pratchett") == "gaiman"
    assert author_surname("Martin Luther King Jr.") == "king"
    assert author_surname("") == ""


def _raw(id_, title, author, *, audio=False, ebook=False, isbn=None, asin=None, added=1000, updated=None,
         subtitle=None, lib="L"):
    media = {"metadata": {"title": title, "subtitle": subtitle, "authorName": author, "isbn": isbn, "asin": asin},
             "numTracks": 3 if audio else 0, "numAudioFiles": 3 if audio else 0, "duration": 100.0 if audio else 0}
    if ebook:
        media["ebookFormat"] = "epub"
    return {"id": id_, "libraryId": lib, "addedAt": added, "updatedAt": updated or added, "size": 10, "media": media}


def _rec(*a, **k):
    return item_record(_raw(*a, **k))


def test_pairs_across_libraries_by_title_and_author():
    items = [
        _rec("a1", "Steve Jobs", "Walter Isaacson", audio=True, lib="audio"),
        _rec("e1", "Steve Jobs", "Walter Isaacson", ebook=True, isbn="9781451648553", lib="books"),
        _rec("a2", "The Poppy War (Unabridged)", "R. F. Kuang", audio=True),
        _rec("e2", "Poppy War", "R.F. Kuang", ebook=True),
        _rec("a3", "Steve Jobs", "Someone Else", audio=True),     # different author: no pair
        _rec("e3", "Different Book", "Walter Isaacson", ebook=True),
    ]
    pairs = {(a, e): r for a, e, r in find_pairs(items)}
    assert pairs == {("a1", "e1"): "title+author", ("a2", "e2"): "title+author"}


def test_pairs_by_identifier_including_isbn10():
    items = [
        _rec("a1", "Totally Different Audio Title", "X Y", audio=True, isbn="0-306-40615-2"),
        _rec("e1", "Print Title", "Z", ebook=True, isbn="978-0-306-40615-7"),
        _rec("a2", "Audio", "Q", audio=True, asin="B0F8NVX5QV"),
        _rec("e2", "Ebook", "R", ebook=True, asin="b0f8nvx5qv"),
    ]
    pairs = {(a, e): r for a, e, r in find_pairs(items)}
    assert pairs == {("a1", "e1"): "isbn", ("a2", "e2"): "asin"}


def test_single_item_with_audio_and_ebook():
    items = [_rec("x", "Dune", "Frank Herbert", audio=True, ebook=True),
             _rec("e", "Dune", "Frank Herbert", ebook=True)]
    assert find_pairs(items) == [("x", "x", "same item")]


def test_non_epub_ebooks_are_ignored():
    r = item_record({"id": "p", "media": {"metadata": {"title": "Dune", "authorName": "Frank Herbert"},
                                          "ebookFormat": "pdf"}})
    a = _rec("a", "Dune", "Frank Herbert", audio=True)
    assert find_pairs([a, r]) == []


# ---------------------------------------------------------------- polling


class FakeLibrary:
    """Mock ABS for the service client: libraries, paged listing, expanded items."""

    def __init__(self):
        self.libs = [{"id": "AUD", "mediaType": "book"}, {"id": "EBK", "mediaType": "book"},
                     {"id": "POD", "mediaType": "podcast"}]
        self.items = {}       # id -> raw minified item
        self.files = {}       # id -> expanded file info
        self.expanded_calls = 0

    def add(self, raw, files):
        self.items[raw["id"]] = raw
        self.files[raw["id"]] = files

    def expanded(self, id_):
        raw = self.items[id_]
        f = self.files[id_]
        media = dict(raw["media"])
        if "tracks" in f:
            media["tracks"] = f["tracks"]
            media["audioFiles"] = []
        if "ebook" in f:
            media["ebookFile"] = f["ebook"]
        return {**raw, "media": media}

    def __call__(self, req: httpx.Request):
        p = req.url.path
        if p == "/api/libraries":
            return httpx.Response(200, json={"libraries": self.libs})
        if p.startswith("/api/libraries/") and p.endswith("/items"):
            lid = p.split("/")[3]
            rows = sorted((r for r in self.items.values() if r["libraryId"] == lid),
                          key=lambda r: r["updatedAt"], reverse=req.url.params.get("desc") == "1")
            limit = int(req.url.params["limit"])
            page = int(req.url.params["page"])
            return httpx.Response(200, json={"results": rows[page * limit:(page + 1) * limit], "total": len(rows)})
        if p.startswith("/api/items/"):
            self.expanded_calls += 1
            return httpx.Response(200, json=self.expanded(p.split("/")[3]))
        return httpx.Response(404)


def _track(ino, size=100, mtime=1):
    return {"index": 1, "ino": ino, "duration": 100.0, "startOffset": 0, "contentUrl": f"/x/{ino}",
            "metadata": {"ext": ".m4a", "size": size, "mtimeMs": mtime}}


def _ebook(ino, size=50, mtime=1):
    return {"ino": ino, "ebookFormat": "epub", "metadata": {"ext": ".epub", "size": size, "mtimeMs": mtime}}


@pytest.fixture
def world(tmp_path):
    s = Settings()
    s.data_dir = tmp_path
    s.ensure_dirs()
    db = DB(s.db_path)
    lib = FakeLibrary()
    lib.add(_raw("a-old", "Old Book", "Ann Author", audio=True, added=100, lib="AUD"), {"tracks": [_track("t1")]})
    lib.add(_raw("e-old", "Old Book", "Ann Author", ebook=True, added=120, lib="EBK"), {"ebook": _ebook("b1")})
    lib.add(_raw("a-new", "New Book", "Bo Writer", audio=True, added=500, lib="AUD"), {"tracks": [_track("t2")]})
    lib.add(_raw("e-new", "New Book: A Novel", "Bo Writer", ebook=True, added=400, lib="EBK"),
            {"ebook": _ebook("b2")})
    lib.add(_raw("a-solo", "Unpaired", "Nobody", audio=True, added=50, lib="AUD"), {"tracks": [_track("t3")]})
    client = ServiceClient("http://abs.test", "key", transport=httpx.MockTransport(lib))
    disc = Discovery(s, db, client=client)
    return s, db, lib, disc


def test_backfill_queues_oldest_first_and_is_idempotent(world):
    s, db, lib, disc = world
    res = disc.poll()
    assert res["full"] is True and res["queued"] == 2 and res["newPairs"] == 2
    first = db.next_queued()
    assert (first["audio_id"], first["ebook_id"]) == ("a-old", "e-old")
    assert first["priority"] == 0 and first["source"] == "auto"
    res2 = disc.poll()
    assert res2["queued"] == 0 and res2["newPairs"] == 0 and res2["full"] is False
    assert len(db.list_jobs()) == 2
    pairs = {(p["audio_id"], p["ebook_id"]) for p in db.list_pairs()}
    assert pairs == {("a-old", "e-old"), ("a-new", "e-new")}


def test_incremental_poll_picks_up_new_item(world):
    s, db, lib, disc = world
    disc.poll()
    lib.add(_raw("e-solo", "Unpaired", "Nobody", ebook=True, added=900, lib="EBK"), {"ebook": _ebook("b3")})
    res = disc.poll()
    assert res["full"] is False and res["queued"] == 1
    assert db.get_job("a-solo", "e-solo")["status"] == "queued"


def _finish(db, s, a, e, lib):
    from nobookends_sync.abs_client import file_signature

    sig = file_signature(lib.expanded(a), lib.expanded(e))
    db.update_job(a, e, status="ready", progress=1.0, signature=sig, result_algo=ALGO_VERSION)


def test_finished_pair_requeued_only_when_files_change(world):
    s, db, lib, disc = world
    disc.poll()
    _finish(db, s, "a-old", "e-old", lib)

    # metadata-only update: updatedAt moves, files identical -> stays ready
    lib.items["e-old"]["updatedAt"] = 2000
    disc.poll()
    assert db.get_job("a-old", "e-old")["status"] == "ready"

    # the EPUB file is replaced (new size/mtime) -> re-queued, old result dropped
    lib.items["e-old"]["updatedAt"] = 3000
    lib.files["e-old"]["ebook"] = _ebook("b1", size=77, mtime=5)
    res = disc.poll()
    assert res["requeuedChanged"] == 1
    j = db.get_job("a-old", "e-old")
    assert j["status"] == "queued" and j["keep_result"] is False


def test_unchanged_items_are_not_refetched(world):
    s, db, lib, disc = world
    disc.poll()
    _finish(db, s, "a-old", "e-old", lib)
    lib.expanded_calls = 0
    disc.poll()
    assert lib.expanded_calls == 0


def test_removed_items_marked_missing_on_full_resync(world):
    s, db, lib, disc = world
    disc.poll()
    del lib.items["a-solo"]
    disc.poll(full=True)
    assert db.get_item("a-solo")["missing"] == 1


def test_user_request_jumps_auto_queue(world):
    from nobookends_sync.jobs import PRIORITY_USER, request_alignment

    s, db, lib, disc = world
    disc.poll()
    request_alignment(db, s, "a-new", "e-new", source="user:alice", priority=PRIORITY_USER)
    nxt = db.next_queued()
    assert (nxt["audio_id"], nxt["ebook_id"]) == ("a-new", "e-new")
