"""API contract and auth tests against a mocked Audiobookshelf."""

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from nobookends_sync import ALGO_VERSION
from nobookends_sync.api import create_app
from nobookends_sync.config import Settings
from nobookends_sync.db import DB
from nobookends_sync.results import save_result

AUDIO = "audio-1"
EBOOK = "ebook-1"
OTHER = "audio-private"

USERS = {
    "tok-good": {"id": "u1", "username": "alice", "access": {AUDIO, EBOOK}},
    "tok-limited": {"id": "u2", "username": "bob", "access": {AUDIO}},
    "svc-key": {"id": "u3", "username": "nobookends-sync", "access": {AUDIO, EBOOK, OTHER}},
}

ITEMS = {
    AUDIO: {"id": AUDIO, "media": {"audioFiles": [
        {"ino": "1", "duration": 100.0}, {"ino": "2", "duration": 50.5}, {"ino": "3", "duration": 9.0, "exclude": True},
    ], "ebookFile": None}},
    EBOOK: {"id": EBOOK, "media": {"audioFiles": [], "ebookFile": {"ino": "9", "ebookFormat": "epub"}}},
    OTHER: {"id": OTHER, "media": {"audioFiles": [{"ino": "4", "duration": 10}], "ebookFile": None}},
}


class FakeAbs:
    def __init__(self):
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        auth = request.headers.get("authorization", "")
        user = USERS.get(auth.removeprefix("Bearer "))
        if user is None:
            return httpx.Response(401, text="Unauthorized")
        if request.url.path == "/api/me":
            return httpx.Response(200, json={"id": user["id"], "username": user["username"], "type": "user"})
        if request.url.path.startswith("/api/items/"):
            item_id = request.url.path.split("/")[3]
            if item_id not in ITEMS or item_id not in user["access"]:
                return httpx.Response(404, text="Not Found")
            return httpx.Response(200, json=ITEMS[item_id])
        return httpx.Response(404)


@pytest.fixture
def env(tmp_path):
    s = Settings()
    s.data_dir = tmp_path
    s.admin_token = "admin-secret"
    s.abs_url = "http://abs.test"
    s.auth_cache_sec = 60
    fake = FakeAbs()
    app = create_app(s, user_transport=httpx.MockTransport(fake), start_background=False, gpu=False)
    with TestClient(app) as client:
        yield client, s, fake


H = {"Authorization": "Bearer tok-good"}


def _make_ready(s: Settings, n=50):
    segs = []
    for k in range(n):
        segs.append({
            "id": k, "t0": k * 2.0, "t1": k * 2.0 + 1.5, "href": "c1.xhtml",
            "cfiStart": f"epubcfi(/6/4[c1]!/4/{2 * k + 2}/1:0)", "cfiEnd": f"epubcfi(/6/4[c1]!/4/{2 * k + 2}/1:10)",
            "text": "Hello you.", "textStart": k * 11, "textEnd": k * 11 + 10,
            "words": [{"t0": k * 2.0, "t1": k * 2.0 + 0.5, "start": 0, "end": 5},
                      {"t0": k * 2.0 + 0.6, "t1": k * 2.0 + 1.5, "start": 6, "end": 9}],
        })
    doc = {"version": 1, "algoVersion": ALGO_VERSION, "audioItemId": AUDIO, "ebookItemId": EBOOK,
           "durationSec": 150.5, "processedSec": 150.5, "quality": {"score": 0.97}, "segments": segs}
    etag = save_result(s.results_dir, AUDIO, EBOOK, doc)
    db = DB(s.db_path)
    db.update_job(AUDIO, EBOOK, status="ready", progress=1.0, result_algo=ALGO_VERSION, result_etag=etag,
                  duration_sec=150.5, finished_at=time.time(), signature="sig")


def test_health_is_public(env):
    client, _, _ = env
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": r.json()["version"], "gpu": False}


def test_requires_bearer(env):
    client, _, fake = env
    assert client.get(f"/api/v1/alignments/{AUDIO}", params={"ebookItemId": EBOOK}).status_code == 401
    r = client.get(f"/api/v1/alignments/{AUDIO}", params={"ebookItemId": EBOOK},
                   headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert client.post("/api/v1/alignments", json={"audioItemId": AUDIO, "ebookItemId": EBOOK}).status_code == 401


def test_rejects_items_user_cannot_access(env):
    client, _, _ = env
    r = client.get(f"/api/v1/alignments/{AUDIO}", params={"ebookItemId": EBOOK},
                   headers={"Authorization": "Bearer tok-limited"})
    assert r.status_code == 404
    r = client.post("/api/v1/alignments", json={"audioItemId": OTHER, "ebookItemId": EBOOK}, headers=H)
    assert r.status_code == 404


def test_none_then_post_queues_idempotently(env):
    client, s, _ = env
    r = client.get(f"/api/v1/alignments/{AUDIO}", params={"ebookItemId": EBOOK}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "none", "progress": 0.0, "version": 1, "audioItemId": AUDIO,
                    "ebookItemId": EBOOK, "durationSec": 150.5, "segments": []}

    r = client.post("/api/v1/alignments", json={"audioItemId": AUDIO, "ebookItemId": EBOOK}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued" and "segments" not in body
    assert body["queuePosition"] == 0
    r2 = client.post("/api/v1/alignments", json={"audioItemId": AUDIO, "ebookItemId": EBOOK}, headers=H)
    assert r2.json()["status"] == "queued"
    jobs = DB(s.db_path).list_jobs()
    assert len(jobs) == 1 and jobs[0]["priority"] == 10


def test_post_validates_item_kinds(env):
    client, _, _ = env
    r = client.post("/api/v1/alignments", json={"audioItemId": EBOOK, "ebookItemId": EBOOK}, headers=H)
    assert r.status_code == 400
    r = client.post("/api/v1/alignments", json={"audioItemId": AUDIO, "ebookItemId": AUDIO}, headers=H)
    assert r.status_code == 400


def test_progress_reported(env):
    client, s, _ = env
    client.post("/api/v1/alignments", json={"audioItemId": AUDIO, "ebookItemId": EBOOK}, headers=H)
    DB(s.db_path).update_job(AUDIO, EBOOK, status="processing", progress=0.42)
    body = client.get(f"/api/v1/alignments/{AUDIO}", params={"ebookItemId": EBOOK}, headers=H).json()
    assert body["status"] == "processing" and body["progress"] == 0.42 and body["segments"] == []


def test_ready_shape_windows_and_etag(env):
    client, s, _ = env
    client.post("/api/v1/alignments", json={"audioItemId": AUDIO, "ebookItemId": EBOOK}, headers=H)
    _make_ready(s)
    r = client.get(f"/api/v1/alignments/{AUDIO}", params={"ebookItemId": EBOOK}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready" and body["progress"] == 1.0 and body["version"] == 1
    assert body["durationSec"] == 150.5 and len(body["segments"]) == 50
    seg = body["segments"][0]
    assert set(seg) >= {"id", "t0", "t1", "href", "cfiStart", "cfiEnd", "text", "words"}
    assert set(seg["words"][0]) == {"t0", "t1", "start", "end"}
    etag = r.headers["etag"]

    r304 = client.get(f"/api/v1/alignments/{AUDIO}", params={"ebookItemId": EBOOK},
                      headers={**H, "If-None-Match": etag})
    assert r304.status_code == 304 and r304.headers["etag"] == etag

    w = client.get(f"/api/v1/alignments/{AUDIO}", params={"ebookItemId": EBOOK, "from": 10, "to": 20}, headers=H)
    ids = [x["id"] for x in w.json()["segments"]]
    assert ids == [5, 6, 7, 8, 9, 10]  # overlapping [10, 20]
    assert w.headers["etag"] != etag
    w2 = client.get(f"/api/v1/alignments/{AUDIO}", params={"ebookItemId": EBOOK, "from": 10.9, "to": 11.0},
                    headers=H)
    assert [x["id"] for x in w2.json()["segments"]] == [5]
    w3 = client.get(f"/api/v1/alignments/{AUDIO}", params={"ebookItemId": EBOOK, "from": 500}, headers=H)
    assert w3.json()["segments"] == []


def test_ready_post_does_not_requeue(env):
    client, s, _ = env
    client.post("/api/v1/alignments", json={"audioItemId": AUDIO, "ebookItemId": EBOOK}, headers=H)
    _make_ready(s)
    body = client.post("/api/v1/alignments", json={"audioItemId": AUDIO, "ebookItemId": EBOOK}, headers=H).json()
    assert body["status"] == "ready" and "segments" not in body
    assert DB(s.db_path).get_job(AUDIO, EBOOK)["status"] == "ready"


def test_auth_results_are_cached(env):
    client, _, fake = env
    for _ in range(5):
        client.get(f"/api/v1/alignments/{AUDIO}", params={"ebookItemId": EBOOK}, headers=H)
    assert fake.calls.count("/api/me") == 1
    assert fake.calls.count(f"/api/items/{AUDIO}") == 1


def test_admin_endpoints(env):
    client, s, _ = env
    assert client.get("/api/v1/admin/jobs").status_code == 401
    assert client.get("/api/v1/admin/jobs", headers=H).status_code == 403
    A = {"Authorization": "Bearer admin-secret"}
    r = client.post("/api/v1/admin/alignments", json={"audioItemId": AUDIO, "ebookItemId": EBOOK,
                                                       "maxDurationSec": 1800}, headers=A)
    assert r.status_code == 200 and r.json()["maxDurationSec"] == 1800 and r.json()["priority"] == 20
    jobs = client.get("/api/v1/admin/jobs", headers=A).json()
    assert jobs["counts"]["queued"] == 1 and jobs["jobs"][0]["audioItemId"] == AUDIO
    _make_ready(s)
    r = client.post(f"/api/v1/admin/jobs/{AUDIO}/{EBOOK}/rerun", json={"maxDurationSec": None}, headers=A)
    assert r.json()["status"] == "queued" and r.json()["keepResult"] is True
    # while re-running, clients keep getting the previous result
    body = client.get(f"/api/v1/alignments/{AUDIO}", params={"ebookItemId": EBOOK}, headers=H).json()
    assert body["status"] == "ready" and body["updating"] is True and len(body["segments"]) == 50
    assert client.get("/api/v1/admin/pairs", headers=A).status_code == 200


def test_admin_disabled_without_token(tmp_path):
    s = Settings()
    s.data_dir = tmp_path
    s.admin_token = ""
    app = create_app(s, user_transport=httpx.MockTransport(FakeAbs()), start_background=False, gpu=False)
    with TestClient(app) as client:
        assert client.get("/api/v1/admin/jobs", headers={"Authorization": "Bearer x"}).status_code == 404


@pytest.fixture
def svc_env(tmp_path):
    """App whose service key can read every item (admin queue validation)."""
    s = Settings()
    s.data_dir = tmp_path
    s.admin_token = "admin-secret"
    s.abs_url = "http://abs.test"
    s.abs_api_key = "svc-key"
    s.auto_window = "01:00-06:00"
    fake = FakeAbs()
    app = create_app(s, user_transport=httpx.MockTransport(fake), service_transport=httpx.MockTransport(fake),
                     start_background=False, gpu=False)
    with TestClient(app) as client:
        yield client, s, fake


A = {"Authorization": "Bearer admin-secret"}
PAIR = {"audioItemId": AUDIO, "ebookItemId": EBOOK}


def test_admin_alignments_priority(svc_env):
    client, s, _ = svc_env
    assert client.post("/api/v1/admin/alignments", json=PAIR).status_code == 401
    assert client.post("/api/v1/admin/alignments", json=PAIR, headers=H).status_code == 403

    # auto priority joins the overnight queue
    r = client.post("/api/v1/admin/alignments", json={**PAIR, "priority": "auto", "source": "allseerr"}, headers=A)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "queued" and j["priority"] == 0 and j["source"] == "admin:allseerr"
    assert isinstance(j["waitingForWindow"], bool)

    # user priority raises it (never lowers); repeating is a no-op
    r = client.post("/api/v1/admin/alignments", json={**PAIR, "priority": "user"}, headers=A)
    assert r.json()["priority"] == 10 and r.json()["waitingForWindow"] is False
    r2 = client.post("/api/v1/admin/alignments", json={**PAIR, "priority": "auto"}, headers=A)
    assert r2.json()["priority"] == 10 and r2.json()["createdAt"] == r.json()["createdAt"]
    # no priority keeps the original admin behavior
    assert client.post("/api/v1/admin/alignments", json=PAIR, headers=A).json()["priority"] == 20
    jobs = client.get("/api/v1/admin/jobs", headers=A).json()
    assert jobs["counts"]["queued"] == 1
    assert jobs["settings"]["autoWindow"] == "01:00-06:00" and "autoWindowOpen" in jobs["settings"]

    # a ready result is not redone
    _make_ready(s)
    assert client.post("/api/v1/admin/alignments", json={**PAIR, "priority": "user"}, headers=A).json()["status"] \
        == "ready"
    assert client.post("/api/v1/admin/alignments", json={**PAIR, "priority": "urgent"}, headers=A).status_code == 422


def test_admin_alignments_validation(svc_env):
    client, s, _ = svc_env

    def post(body):
        return client.post("/api/v1/admin/alignments", json=body, headers=A)

    r = post({"audioItemId": "nope", "ebookItemId": EBOOK})
    assert r.status_code == 404 and r.json()["reason"] == "not_found"
    r = post({"audioItemId": EBOOK, "ebookItemId": EBOOK})
    assert r.status_code == 422 and r.json() == {"detail": "audioItemId has no audio", "reason": "no_audio"}
    r = post({"audioItemId": AUDIO, "ebookItemId": OTHER})
    assert r.status_code == 422 and r.json()["reason"] == "no_epub"
    assert post({"audioItemId": AUDIO}).json()["reason"] == "missing_ids"
    assert post({**PAIR, "pairs": [PAIR]}).json()["reason"] == "ambiguous"
    assert client.get("/api/v1/admin/jobs", headers=A).json()["counts"]["queued"] == 0

    s.abs_api_key = "wrong-key"
    r = post(PAIR)
    assert r.status_code == 502 and r.json()["reason"] == "abs_auth"


def test_admin_alignments_bulk(svc_env):
    client, s, fake = svc_env
    body = {"priority": "auto", "pairs": [
        PAIR, {"audioItemId": AUDIO, "ebookItemId": OTHER}, {"audioItemId": "nope", "ebookItemId": EBOOK}, PAIR,
    ]}
    r = client.post("/api/v1/admin/alignments", json=body, headers=A)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["accepted"] == 2 and d["rejected"] == 2
    assert [x["ok"] for x in d["results"]] == [True, False, False, True]
    assert d["results"][0]["status"] == "queued" and d["results"][0]["priority"] == 0
    assert d["results"][1]["reason"] == "no_epub" and d["results"][2]["reason"] == "not_found"
    assert fake.calls.count(f"/api/items/{AUDIO}") == 1  # each item fetched once per call
    too_many = {"pairs": [PAIR] * 501}
    r = client.post("/api/v1/admin/alignments", json=too_many, headers=A)
    assert r.status_code == 422 and r.json()["reason"] == "too_many_pairs"


def test_admin_status(svc_env):
    client, s, _ = svc_env
    assert client.get("/api/v1/admin/status").status_code == 401
    d = client.get("/api/v1/admin/status", headers=A).json()
    assert d["window"]["start"] == "01:00" and d["window"]["end"] == "06:00"
    assert isinstance(d["window"]["active"], bool)
    assert d["queue"] == {"user": 0, "auto": 0, "total": 0} and d["current"] is None
    assert d["gpu"] is False and d["version"]
    client.post("/api/v1/admin/alignments", json={**PAIR, "priority": "auto"}, headers=A)
    client.post("/api/v1/admin/alignments", json={"audioItemId": AUDIO, "ebookItemId": AUDIO + "x"}, headers=A)
    assert client.get("/api/v1/admin/status", headers=A).json()["queue"] == {"user": 0, "auto": 1, "total": 1}
    s.auto_window = "always"
    assert client.get("/api/v1/admin/status", headers=A).json()["window"] == {"start": None, "end": None,
                                                                           "active": True}


def test_invalid_auto_window_fails_at_startup(tmp_path):
    s = Settings()
    s.data_dir = tmp_path
    s.auto_window = "nightly"
    with pytest.raises(SystemExit):
        create_app(s, start_background=False, gpu=False)
