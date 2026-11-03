"""User data sync (contract v1, 2026-10-14) against a mocked Audiobookshelf."""

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from nobookends_sync import __version__
from nobookends_sync import api as api_mod
from nobookends_sync.api import create_app
from nobookends_sync.config import Settings
from nobookends_sync.db import DB
from nobookends_sync.userdata import COLLECTIONS, MAX_ITEM_BYTES, TOMBSTONE_TTL_MS

USERS = {"tok-a": {"id": "u1", "username": "alice"}, "tok-a2": {"id": "u1", "username": "alice"},
         "tok-b": {"id": "u2", "username": "bob"}}


class FakeAbs:
    def __init__(self):
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        user = USERS.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        if user is None:
            return httpx.Response(401, text="Unauthorized")
        if request.url.path == "/api/me":
            return httpx.Response(200, json={"id": user["id"], "username": user["username"], "type": "user"})
        if request.url.path == "/api/items/a":
            return httpx.Response(200, json={"id": "a", "media": {"audioFiles": [{"ino": "1", "duration": 5}]}})
        if request.url.path == "/api/items/e":
            return httpx.Response(200, json={"id": "e", "media": {"ebookFile": {"ino": "2", "ebookFormat": "epub"}}})
        return httpx.Response(404)


def _settings(tmp_path, alignment=True):
    s = Settings()
    s.data_dir = tmp_path
    s.admin_token = "admin-secret"
    s.abs_url = "http://abs.test"
    s.abs_api_key = ""
    s.alignment_enabled = alignment
    return s


@pytest.fixture
def env(tmp_path):
    s = _settings(tmp_path)
    fake = FakeAbs()
    app = create_app(s, user_transport=httpx.MockTransport(fake), start_background=False, gpu=False)
    with TestClient(app) as client:
        yield client, s, fake


A = {"Authorization": "Bearer tok-a"}
A2 = {"Authorization": "Bearer tok-a2"}
B = {"Authorization": "Bearer tok-b"}
URL = "/api/v1/me/data/ratings"


def item(i, ts, **data):
    return {"id": str(i), "updatedAt": ts, "data": data or {"stars": 4}}


def post(client, items, headers=A, url=URL):
    return client.post(url, json={"items": items}, headers=headers)


# ------------------------------------------------------------------ capabilities


def test_capabilities_public_alignment_on(env):
    client, _, fake = env
    r = client.get("/api/v1/capabilities")
    assert r.status_code == 200
    assert r.json() == {"version": __version__, "alignment": True, "userData": True,
                        "collections": ["podcastSubscriptions", "podcastProgress", "ratings", "highlights", "clips",
                                        "readingState", "appSettings", "achievements", "listeningPaths"]}
    assert fake.calls == 0
    assert set(client.get("/api/v1/health").json()) == {"ok", "version", "gpu"}


def test_capabilities_alignment_off(tmp_path):
    app = create_app(_settings(tmp_path, alignment=False), user_transport=httpx.MockTransport(FakeAbs()),
                     start_background=False, gpu=False)
    with TestClient(app) as client:
        assert client.get("/api/v1/capabilities").json()["alignment"] is False


def test_data_works_with_worker_disabled(tmp_path):
    """start_background on but alignment off: no worker, no discovery, data and health still fine."""
    s = _settings(tmp_path, alignment=False)
    app = create_app(s, user_transport=httpx.MockTransport(FakeAbs()), start_background=True, gpu=False)
    with TestClient(app) as client:
        assert app.state.nbs["worker"] is None and app.state.nbs["discovery"] is None
        assert client.get("/api/v1/health").json()["ok"] is True
        assert post(client, [item(1, 1000)]).json()["applied"] == ["1"]
        r = client.get(URL, headers=A).json()
        assert [i["id"] for i in r["items"]] == ["1"]
        # alignment requests say so instead of queueing forever
        r = client.post("/api/v1/alignments", json={"audioItemId": "a", "ebookItemId": "e"}, headers=A)
        assert r.status_code == 503 and r.json()["reason"] == "alignment_disabled"


# ------------------------------------------------------------------ auth and collections


def test_auth_required(env):
    client, _, _ = env
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post(URL, json={"items": []}).status_code == 401
    assert client.post(URL, json={"items": []}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.delete("/api/v1/me/data").status_code == 401
    assert client.delete("/api/v1/me/data", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_collection_allow_list(env):
    client, _, _ = env
    for c in COLLECTIONS:
        assert client.get(f"/api/v1/me/data/{c}", headers=A).status_code == 200
    assert client.get("/api/v1/me/data/secrets", headers=A).status_code == 404
    assert post(client, [item(1, 1)], url="/api/v1/me/data/secrets").status_code == 404


def test_users_are_isolated_and_keyed_by_abs_user_id(env):
    client, _, _ = env
    post(client, [item("x", 10, stars=5)])
    assert client.get(URL, headers=B).json() == {"items": [], "seq": 0, "more": False}
    # a second token of the same ABS user sees the same data
    r = client.get(URL, headers=A2).json()
    assert r["items"] == [{"id": "x", "data": {"stars": 5}, "updatedAt": 10, "deleted": False, "seq": 1}]
    # collections are separate
    assert client.get("/api/v1/me/data/clips", headers=A).json()["items"] == []


# ------------------------------------------------------------------ last write wins


def test_lww_newer_applies_older_and_tie_are_stale(env):
    client, _, _ = env
    r = post(client, [item("a", 100, stars=1)]).json()
    assert r == {"applied": ["a"], "rejected": [], "seq": 1}
    r = post(client, [item("a", 200, stars=2)]).json()
    assert r == {"applied": ["a"], "rejected": [], "seq": 2}
    r = post(client, [item("a", 150, stars=9)]).json()
    assert r == {"applied": [], "rejected": [{"id": "a", "reason": "stale"}], "seq": 2}
    r = post(client, [item("a", 200, stars=7)]).json()  # tie keeps the stored one
    assert r == {"applied": [], "rejected": [{"id": "a", "reason": "stale"}], "seq": 2}
    got = client.get(URL, headers=A).json()["items"]
    assert got == [{"id": "a", "data": {"stars": 2}, "updatedAt": 200, "deleted": False, "seq": 2}]


def test_seq_is_per_user_and_monotonic_across_collections(env):
    client, _, _ = env
    post(client, [item("a", 1), item("b", 1)])
    r = post(client, [item("p", 1)], url="/api/v1/me/data/podcastProgress").json()
    assert r["seq"] == 3
    r = client.get(URL, headers=A, params={"since": 1}).json()
    assert [i["id"] for i in r["items"]] == ["b"] and r["seq"] == 2
    assert post(client, [item("z", 1)], headers=B).json()["seq"] == 1


def test_same_id_twice_in_one_post(env):
    client, _, _ = env
    r = post(client, [item("a", 5, stars=1), item("a", 4, stars=2), item("a", 6, stars=3)]).json()
    assert r["applied"] == ["a", "a"]
    assert r["rejected"] == [{"id": "a", "reason": "stale"}]
    assert client.get(URL, headers=A).json()["items"][0]["data"] == {"stars": 3}


# ------------------------------------------------------------------ tombstones


def test_delete_is_tombstone_and_purged_after_90_days(env):
    client, s, _ = env
    post(client, [item("a", 10), item("b", 10)])
    r = post(client, [{"id": "a", "updatedAt": 20, "deleted": True, "data": {"ignored": 1}}]).json()
    assert r["applied"] == ["a"] and r["seq"] == 3
    r = client.get(URL, headers=A, params={"since": 2}).json()
    assert r["items"] == [{"id": "a", "data": None, "updatedAt": 20, "deleted": True, "seq": 3}]
    # a stale write cannot resurrect it; a newer one can
    assert post(client, [item("a", 15)]).json()["rejected"] == [{"id": "a", "reason": "stale"}]
    # delete of an unknown id is stored too, so other devices learn about it
    assert post(client, [{"id": "never", "updatedAt": 1, "deleted": True}]).json()["applied"] == ["never"]

    db = DB(s.db_path)
    now = int(time.time() * 1000)
    assert db.ud_purge_tombstones(now - TOMBSTONE_TTL_MS) == 0  # fresh tombstones stay
    with db.conn() as c:
        c.execute("UPDATE user_data SET stored_at=? WHERE id='a'", (now - TOMBSTONE_TTL_MS - 1000,))
    assert db.ud_purge_tombstones(now - TOMBSTONE_TTL_MS) == 1
    ids = [i["id"] for i in client.get(URL, headers=A).json()["items"]]
    assert ids == ["b", "never"]
    # cursor never goes backwards even though the newest row was purged
    assert client.get(URL, headers=A, params={"since": 99}).json()["seq"] == 99


# ------------------------------------------------------------------ paging


def test_paging_with_more(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setattr(api_mod, "PAGE_SIZE", 3)
    post(client, [item(i, 1) for i in range(7)])
    seen, since, calls = [], 0, 0
    while True:
        r = client.get(URL, headers=A, params={"since": since}).json()
        calls += 1
        seen += [i["id"] for i in r["items"]]
        since = r["seq"]
        if not r["more"]:
            break
    assert seen == [str(i) for i in range(7)] and calls == 3 and since == 7
    first = client.get(URL, headers=A).json()
    assert first["more"] is True and first["seq"] == 3


def test_paging_real_page_size(env):
    client, _, _ = env
    for chunk in range(3):
        post(client, [item(f"{chunk}-{i}", 1) for i in range(500)])
    r = client.get(URL, headers=A).json()
    assert len(r["items"]) == 1000 and r["more"] is True and r["seq"] == 1000
    r = client.get(URL, headers=A, params={"since": r["seq"]}).json()
    assert len(r["items"]) == 500 and r["more"] is False and r["seq"] == 1500


def test_since_must_be_non_negative(env):
    client, _, _ = env
    assert client.get(URL, headers=A, params={"since": -1}).status_code == 422


# ------------------------------------------------------------------ limits and validation


def test_too_many_items_is_413(env):
    client, _, _ = env
    r = post(client, [item(i, 1) for i in range(501)])
    assert r.status_code == 413
    assert client.get(URL, headers=A).json()["items"] == []


def test_item_too_large(env):
    client, _, _ = env
    big = {"blob": "x" * MAX_ITEM_BYTES}
    r = post(client, [{"id": "big", "updatedAt": 1, "data": big}, item("ok", 1)]).json()
    assert r["applied"] == ["ok"]
    assert r["rejected"] == [{"id": "big", "reason": "too_large"}]
    fits = {"b": "x" * (MAX_ITEM_BYTES - len('{"b":""}'))}
    assert post(client, [{"id": "edge", "updatedAt": 1, "data": fits}]).json()["applied"] == ["edge"]


def test_invalid_items_rejected_individually(env):
    client, _, _ = env
    r = post(client, [
        {"id": "", "updatedAt": 1, "data": {}},
        {"id": "x" * 201, "updatedAt": 1, "data": {}},
        {"id": "nots", "updatedAt": "yesterday", "data": {}},
        {"id": "bool", "updatedAt": True, "data": {}},
        {"id": "frac", "updatedAt": 1.5, "data": {}},
        {"id": "nodata", "updatedAt": 1},
        {"id": "list", "updatedAt": 1, "data": [1, 2]},
        {"id": "deleted-str", "updatedAt": 1, "deleted": "yes"},
        "not an object",
        {"id": "x" * 200, "updatedAt": 1.0, "data": {}},
    ]).json()
    assert r["applied"] == ["x" * 200]
    reasons = [(x["id"], x["reason"]) for x in r["rejected"]]
    assert reasons == [("", "invalid"), ("x" * 201, "invalid"), ("nots", "invalid"), ("bool", "invalid"),
                       ("frac", "invalid"), ("nodata", "invalid"), ("list", "invalid"), ("deleted-str", "invalid"),
                       (None, "invalid")]
    assert client.post(URL, json={"nope": []}, headers=A).status_code == 422


def test_quota_counts_live_items_only(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setattr(api_mod, "MAX_LIVE_ITEMS", 3)
    r = post(client, [item(i, 1) for i in range(4)]).json()
    assert r["applied"] == ["0", "1", "2"] and r["rejected"] == [{"id": "3", "reason": "quota"}]
    # updating a live item and deleting are fine at the limit
    assert post(client, [item("0", 2)]).json()["applied"] == ["0"]
    assert post(client, [{"id": "1", "updatedAt": 2, "deleted": True}]).json()["applied"] == ["1"]
    # a delete frees a slot
    assert post(client, [item("3", 1)]).json()["applied"] == ["3"]
    # quota is per collection and per user
    assert post(client, [item("9", 1)], url="/api/v1/me/data/clips").json()["applied"] == ["9"]
    assert post(client, [item("9", 1)], headers=B).json()["applied"] == ["9"]


def test_body_over_8mb_is_413(env):
    client, _, _ = env
    body = json.dumps({"items": [{"id": "a", "updatedAt": 1, "data": {"b": "x" * (8 * 1024 * 1024)}}]})
    r = client.post(URL, content=body, headers={**A, "Content-Type": "application/json"})
    assert r.status_code == 413

    def chunks():
        for _ in range(9):
            yield b" " * (1024 * 1024)

    r = client.post(URL, content=chunks(), headers={**A, "Content-Type": "application/json"})
    assert r.status_code == 413


# ------------------------------------------------------------------ wipe and admin


def test_wipe_deletes_only_callers_data(env):
    client, _, _ = env
    post(client, [item("a", 1), item("b", 1)])
    post(client, [{"id": "b", "updatedAt": 2, "deleted": True}])
    post(client, [item("c", 1)], url="/api/v1/me/data/clips")
    post(client, [item("z", 1)], headers=B)
    r = client.delete("/api/v1/me/data", headers=A)
    assert r.status_code == 200 and r.json() == {"deleted": 3}
    for c in COLLECTIONS:
        assert client.get(f"/api/v1/me/data/{c}", headers=A).json()["items"] == []
    assert [i["id"] for i in client.get(URL, headers=B).json()["items"]] == ["z"]
    # new writes after a wipe still get higher seqs than before
    assert post(client, [item("n", 1)]).json()["seq"] == 5


def test_admin_status_has_user_data_counts(env):
    client, _, _ = env
    post(client, [item("a", 1), item("b", 1)])
    post(client, [{"id": "b", "updatedAt": 2, "deleted": True}])
    post(client, [item("z", 1)], headers=B)
    post(client, [item("c", 1)], url="/api/v1/me/data/clips")
    r = client.get("/api/v1/admin/status", headers={"Authorization": "Bearer admin-secret"}).json()
    assert {"version", "gpu", "window", "queue", "current"} <= set(r)
    ud = r["userData"]
    assert ud["users"] == 2
    assert ud["collections"]["ratings"] == {"items": 2, "deleted": 1, "users": 2}
    assert ud["collections"]["clips"] == {"items": 1, "deleted": 0, "users": 1}
    assert ud["collections"]["highlights"] == {"items": 0, "deleted": 0, "users": 0}
    assert set(ud["collections"]) == set(COLLECTIONS)


def test_me_lookup_is_cached(env):
    client, _, fake = env
    for _ in range(3):
        client.get(URL, headers=A)
    assert fake.calls == 1


def test_reading_state_collection(env):
    """1.3.0: the app's last mode per book syncs like any other collection, per user."""
    client, _, _ = env
    url = "/api/v1/me/data/readingState"
    rec = {"id": "work-1", "updatedAt": 10, "data": {"mode": "listenRead", "at": 10, "ids": ["work-1", "eb-1"]}}
    r = post(client, [rec], url=url).json()
    assert r["applied"] == ["work-1"]
    # last write wins: an older write is stale, a newer one replaces it
    older = {**rec, "updatedAt": 5, "data": {"mode": "read", "at": 5, "ids": ["work-1"]}}
    assert post(client, [older], url=url).json()["rejected"] == [{"id": "work-1", "reason": "stale"}]
    newer = {**rec, "updatedAt": 20, "data": {"mode": "listen", "at": 20, "ids": ["work-1"]}}
    assert post(client, [newer], url=url).json()["applied"] == ["work-1"]
    items = client.get(url, headers=A).json()["items"]
    assert [(i["id"], i["data"]["mode"]) for i in items] == [("work-1", "listen")]
    # a folded entry is deleted with a tombstone
    post(client, [{"id": "eb-1", "updatedAt": 1, "data": {"mode": "read", "at": 1, "ids": []}}], url=url)
    post(client, [{"id": "eb-1", "updatedAt": 2, "deleted": True}], url=url)
    gone = [i for i in client.get(url, headers=A).json()["items"] if i["id"] == "eb-1"]
    assert gone and gone[0]["deleted"] is True
    # other users never see it, and it is not mixed into ratings
    assert client.get(url, headers=B).json()["items"] == []
    assert client.get(URL, headers=A).json()["items"] == []
    assert "readingState" in client.get("/api/v1/capabilities").json()["collections"]


def test_app_settings_collection(env):
    """1.4.0: the app's settings sync between one user's devices, last write wins."""
    client, _, _ = env
    url = "/api/v1/me/data/appSettings"
    rec = {"id": "settings", "updatedAt": 10, "data": {"v": 1, "settings": {"skipBackSeconds": 15}}}
    assert post(client, [rec], url=url).json()["applied"] == ["settings"]
    newer = {**rec, "updatedAt": 20, "data": {"v": 1, "settings": {"skipBackSeconds": 30}}}
    assert post(client, [newer], url=url).json()["applied"] == ["settings"]
    items = client.get(url, headers=A).json()["items"]
    assert [(i["id"], i["data"]["settings"]["skipBackSeconds"]) for i in items] == [("settings", 30)]


def test_achievements_collection(env):
    """1.5.0: earned Shelf badges sync per user, last write wins."""
    client, _, _ = env
    url = "/api/v1/me/data/achievements"
    rec = {"id": "first-book", "updatedAt": 10, "data": {"earnedAt": 10}}
    assert post(client, [rec], url=url).json()["applied"] == ["first-book"]
    items = client.get(url, headers=A).json()["items"]
    assert [i["id"] for i in items] == ["first-book"]


def test_listening_paths_collection(env):
    """1.6.0: the chosen path per multi-path audiobook syncs per user, last write wins."""
    client, _, _ = env
    url = "/api/v1/me/data/listeningPaths"
    rec = {"id": "li_dead_girls", "updatedAt": 10, "data": {"path": "viola"}}
    assert post(client, [rec], url=url).json()["applied"] == ["li_dead_girls"]
    items = client.get(url, headers=A).json()["items"]
    assert [i["id"] for i in items] == ["li_dead_girls"]
