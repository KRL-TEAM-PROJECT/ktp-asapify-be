import base64
import copy
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from asapify_api.app import app
from asapify_api.auth import require_operator
from asapify_api import auth, delivery
from asapify_api.core import ApiProblem, DecisionInput, as_time, check_decidable, geojson, night_of, night_slots, validate_grid
from asapify_api.repository import get_repository

INDEX = {"version": "test-only", "origin": [94.9, 6.2], "step": .02,
         "rows": [420, 421], "cols": [951, 951], "province": [62, 62]}
GRID = {"pixels_version": "test-only", "slot": "2026-10-08T13:00:00Z", "n_sat": 2,
        "status": "AN", "utility": base64.b64encode(bytes([200, 255])).decode(),
        "clusters": {"C-1008-test": [0]}, "pixel_n_sat": [2, 0]}
CLUSTER = {"id": "C-1008-test", "state": "active", "province": "62", "decision": None,
           "verification": {"result": "inconclusive", "images": [{"source": "himawari_image", "url": "https://example.test/evidence.png"}]}}


class FakeRepo:
    def __init__(self):
        self.value = copy.deepcopy(CLUSTER)
        self.saves = 0
        self.completed = None

    def cluster(self, cid, at=None):
        return copy.deepcopy(self.value)

    def decide(self, cid, body, actor):
        check_decidable(self.value, body)
        self.saves += 1
        d = {"action": body.action, "by": actor["email"], "at": GRID["slot"],
             "reason": body.reason, "alert_id": "A-1008-test" if body.action == "publish" else None,
             "telegram": {"status": "pending", "message_id": None} if body.action == "publish" else None}
        self.value["decision"] = d
        return d, self.value

    def finish_delivery(self, cid, aid, outcome):
        self.completed = outcome

    def telegram_chat(self, province):
        return "-100123"

    def snapshot(self, at):
        return {"timestamp": GRID["slot"], "n_sat": 2, "archive_first": "2026-10-08",
                "pixels_object": "api/pixels.json", "grid_object": "api/grid.json", "replay": False}

    def json_object(self, name):
        return copy.deepcopy(INDEX if "pixels" in name else GRID)

    def night(self, night):
        return None


@pytest.fixture(autouse=True)
def reset():
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def client(repo=None, operator=False):
    if repo:
        app.dependency_overrides[get_repository] = lambda: repo
    if operator:
        app.dependency_overrides[require_operator] = lambda: {"uid": "test", "email": "operator@example.test"}
    return TestClient(app, raise_server_exceptions=False)


def test_health_without_credentials():
    r = client().get("/api/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


@pytest.mark.parametrize("path", ["pixels", "grid", "nights?month=2026-10", "basemaps", "clusters", "clusters/test", "alerts"])
def test_operator_reads_require_auth_before_data(path):
    r = client().get("/api/v1/operator/" + path)
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"


def test_invalid_token(monkeypatch):
    monkeypatch.setattr(auth, "firebase_app", lambda: None)
    def invalid(*a, **kw):
        raise ValueError("invalid JWT")
    monkeypatch.setattr(auth.auth, "verify_id_token", invalid)
    r = client().get("/api/v1/operator/pixels", headers={"Authorization": "Bearer forged"})
    assert r.status_code == 401


def test_signed_user_without_role_is_forbidden(monkeypatch):
    monkeypatch.setattr(auth, "firebase_app", lambda: None)
    monkeypatch.setattr(auth.auth, "verify_id_token", lambda *a, **kw: {"uid": "a", "email_verified": True})
    assert client().get("/api/v1/operator/pixels", headers={"Authorization": "Bearer valid"}).status_code == 403


def test_revocation_check_is_requested(monkeypatch):
    monkeypatch.setattr(auth, "firebase_app", lambda: None)
    seen = {}
    def verify(*a, **kw):
        seen.update(kw)
        return {"uid": "test", "role": "operator", "email_verified": True}
    monkeypatch.setattr(auth.auth, "verify_id_token", verify)
    r = client(FakeRepo()).get("/api/v1/operator/pixels", headers={"Authorization": "Bearer valid"})
    assert r.status_code == 200 and seen["check_revoked"] is True


def test_no_data_is_not_safe():
    class Empty(FakeRepo):
        def snapshot(self, at):
            raise ApiProblem(503, "DATA_NOT_READY", "No data")
    r = client(Empty(), True).get("/api/v1/operator/grid?format=compact")
    assert r.status_code == 503 and "SAFE" not in r.text


def test_grid_preserves_missing_data_and_lon_lat():
    data = geojson(INDEX, GRID)
    assert data["features"][1]["properties"]["status"] == "NO_OBSERVABLE"
    assert data["features"][1]["properties"]["utility"] is None
    assert data["features"][0]["geometry"]["coordinates"] == pytest.approx([113.93, -2.21])


@pytest.mark.parametrize("mutation", [
    {"pixels_version": "wrong"}, {"utility": "invalid"}, {"status": "AS"},
    {"n_sat": 1}, {"clusters": {"bad": [2]}},
])
def test_invalid_grid_is_rejected(mutation):
    with pytest.raises(ApiProblem) as e:
        validate_grid(INDEX, {**GRID, **mutation})
    assert e.value.code == "DATA_INVALID"


def test_night_is_54_slots_and_midnight_belongs_to_previous_evening():
    assert len(night_slots("2026-10-08")) == 54
    assert night_slots("2026-10-08")[-1] == "2026-10-08T21:50:00Z"
    assert night_of(as_time("2026-10-08T18:00:00Z")) == "2026-10-08"


@pytest.mark.parametrize("value", ["2026-10-08", "2026-10-08T13:00:00", "2026-10-08T20:00:00+07:00", "2026-13-01T13:00:00Z"])
def test_as_of_requires_utc(value):
    assert client(FakeRepo(), True).get("/api/v1/operator/pixels", params={"as_of": value}).status_code == 400


def test_reject_reason_required_and_no_cloud_mutation():
    repo = FakeRepo()
    r = client(repo, True).post("/api/v1/operator/clusters/test/decision", json={"action": "reject", "reason": " "})
    assert r.status_code == 422 and repo.saves == 0


def test_reject_is_saved_without_telegram():
    repo = FakeRepo()
    r = client(repo, True).post("/api/v1/operator/clusters/test/decision", json={"action": "reject", "reason": "needs review"})
    assert r.status_code == 201 and repo.saves == 1 and r.json()["telegram"] is None


def test_publication_is_preserved_when_queue_is_unavailable(monkeypatch):
    import importlib
    module = importlib.import_module("asapify_api.app")
    def unavailable():
        raise RuntimeError("not configured")
    monkeypatch.setattr(module, "get_tasks", unavailable)
    repo = FakeRepo()
    r = client(repo, True).post("/api/v1/operator/clusters/test/decision", json={"action": "publish", "caption": "WARNING; not confirmed"})
    assert r.status_code == 201 and repo.saves == 1
    assert r.json()["queue"]["state"] == "awaiting_recovery"


def test_consecutive_decisions_reserve_and_enqueue_only_once(monkeypatch):
    import importlib
    module = importlib.import_module("asapify_api.app")
    repo = FakeRepo()
    calls = []
    monkeypatch.setattr(module, "get_tasks", lambda: object())
    store = Mock()
    store.fan_out.return_value = {"state": "outbox_saved", "recipients": 41, "jobs": 41}
    monkeypatch.setattr(module, "get_notification_store", lambda: store)
    def enqueue(*args):
        assert repo.saves == 1 and store.fan_out.call_count == 1  # recipients exist before tasks are queued
        calls.append(1)
        return 1
    monkeypatch.setattr(module, "enqueue_pending", enqueue)
    c = client(repo, True)
    body = {"action": "publish", "caption": "WARNING; not confirmed"}
    r = c.post("/api/v1/operator/clusters/test/decision", json=body)
    assert r.status_code == 201 and r.json()["notifications"]["recipients"] == 41
    assert c.post("/api/v1/operator/clusters/test/decision", json=body).status_code == 409
    assert len(calls) == 1
    store.fan_out.assert_called_once_with("A-1008-test")


def test_telegram_timeout_is_unknown_and_is_not_retried(monkeypatch):
    calls = []
    def post(*a, **kw):
        calls.append(1)
        raise httpx.ReadTimeout("request with secret URL")
    monkeypatch.setattr(httpx.Client, "post", post)
    result = delivery.send_photo(("secret", "chat", "https://example.test/photo"), "WARNING")
    assert result["status"] == "unknown" and len(calls) == 1 and "secret" not in str(result)


def test_decision_cannot_override_rule_status():
    r = client(FakeRepo(), True).post("/api/v1/operator/clusters/test/decision", json={"action": "reject", "reason": "test", "status": "SAFE"})
    assert r.status_code == 400


def test_pixel_index_cache_is_private_and_requires_revalidation():
    c = client(FakeRepo(), True)
    r = c.get("/api/v1/operator/pixels")
    assert r.headers["cache-control"] == "private, no-cache"
    assert c.get("/api/v1/operator/pixels", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
