"""Operator flow per region: Re-verify, agent quota, publish/reject and notification fan-out."""
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from asapify_api import engine_routes
from asapify_api.app import app
from asapify_api.auth import require_operator
from asapify_api.core import utc_now
from asapify_api.engine_routes import get_engine_repo
from asapify_engine.regions import load_regions
from test_engine_ingest import FakeRepo
from test_notifications import count, fanout_db
from test_verifier import RECORD, SETTINGS, TOOL, YAML_OK, fake_run

REGION = load_regions()[0]


@pytest.fixture
def repo(monkeypatch):
    repo = FakeRepo()
    repo.docs[REGION.id] = {"id": REGION.id, "status": "ALERT", "latest": {**RECORD}}
    app.dependency_overrides[get_engine_repo] = lambda: repo
    app.dependency_overrides[require_operator] = lambda: {"uid": "op", "email": "operator@example.test"}
    monkeypatch.setattr(engine_routes, "Settings", lambda: SETTINGS)
    real = engine_routes.verify_region
    monkeypatch.setattr(engine_routes, "verify_region", lambda *a, **kw: real(
        *a, **{**kw, "settings": SETTINGS, "run": fake_run(YAML_OK, [TOOL], region=REGION)}))
    yield repo
    app.dependency_overrides.clear()


def client():
    return TestClient(app, raise_server_exceptions=False)


def test_reverify_runs_the_agent_once_and_reports_the_quota(repo):
    c = client()
    first = c.post(f"/api/v1/operator/regions/{REGION.id}/verify")
    assert first.status_code == 200 and first.json()["validation_status"] == "CONFIRMED"
    assert first.json()["trigger"] == "manual" and first.json()["requested_by"] == "operator@example.test"
    assert c.post(f"/api/v1/operator/regions/{REGION.id}/verify").json()["error"]["code"] == "VERIFIED_RECENTLY"
    agent = c.get("/api/v1/operator/agent").json()
    assert agent == {"configured": True, "model": "gemini-test", "daily_quota": 2, "used_today": 1,
                     "remaining": 1, "max_tool_calls": 3}


def test_quota_exhausted_is_a_clear_429(repo):
    repo.quota = {utc_now().date(): 2}
    r = client().post(f"/api/v1/operator/regions/{REGION.id}/verify")
    assert r.status_code == 429 and r.json()["error"]["code"] == "AGENT_QUOTA_EXHAUSTED"


def test_region_without_an_assessed_hour_cannot_be_verified(repo):
    repo.docs[REGION.id] = {"id": REGION.id}
    assert client().post(f"/api/v1/operator/regions/{REGION.id}/verify").status_code == 409


def published(repo, monkeypatch, caption="ALERT: thermal indication; verify in the field."):
    store = Mock()
    store.fan_out.return_value = {"state": "outbox_saved", "groups": 1, "jobs": 1}
    monkeypatch.setattr(engine_routes, "get_notification_store", lambda: store)
    monkeypatch.setattr(engine_routes, "get_tasks", lambda: object())
    monkeypatch.setattr(engine_routes, "enqueue_pending", lambda store, tasks: 3)
    c = client()
    vid = c.post(f"/api/v1/operator/regions/{REGION.id}/verify").json()["id"]
    body = {"action": "publish", "caption": caption, "verification_id": vid}
    return c, store, vid, body


def test_publish_creates_one_alert_and_fans_out_once(repo, monkeypatch):
    c, store, vid, body = published(repo, monkeypatch)
    r = c.post(f"/api/v1/operator/regions/{REGION.id}/decision", json=body)
    assert r.status_code == 201 and r.json()["notifications"]["groups"] == 1
    alert_id = f"A-{REGION.id}-{vid}"
    alert = repo.alerts[alert_id]
    assert alert["region_id"] == REGION.id and alert["province"] == REGION.province and alert["purpose"] == "emergency"
    assert alert["source_path"] == f"engine_regions/{REGION.id}/verifications/{vid}"
    assert c.post(f"/api/v1/operator/regions/{REGION.id}/decision", json=body).json()["error"]["code"] == "ALREADY_DECIDED"
    store.fan_out.assert_called_once_with(alert_id)
    assert repo.audit[0]["operator_uid"] == "op" and repo.audit[0]["verification_id"] == vid


def test_rejected_verification_cannot_be_published(repo, monkeypatch):
    c, store, vid, body = published(repo, monkeypatch)
    repo.verifications[REGION.id][-1]["validation_status"] = "REJECTED"
    r = c.post(f"/api/v1/operator/regions/{REGION.id}/decision", json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "VERIFICATION_REJECTED"
    reject = {"action": "reject", "reason": "agent rejected; no field report", "verification_id": vid}
    assert c.post(f"/api/v1/operator/regions/{REGION.id}/decision", json=reject).status_code == 201
    assert not repo.alerts and store.fan_out.call_count == 0


def test_decision_validation(repo, monkeypatch):
    c, _, vid, body = published(repo, monkeypatch)
    url = f"/api/v1/operator/regions/{REGION.id}/decision"
    assert c.post(url, json={**body, "caption": " "}).status_code == 422
    assert c.post(url, json={**body, "verification_id": "20991231T000000Z"}).status_code == 404
    assert c.post(url, json={**body, "verification_id": "../../x"}).status_code == 400
    assert c.post(url, json={**body, "status": "SAFE"}).status_code == 400   # the decision cannot set a level


def test_region_alert_fan_out_reaches_the_groups_and_updates_the_verification():
    from asapify_api.notification_store import NotificationStore
    db, source = fanout_db(2), f"engine_regions/{REGION.id}/verifications/20261013T060000Z"
    db.docs["alerts/A-test"].update({"region_id": REGION.id, "source_path": source})
    db.docs[source] = {"id": "20261013T060000Z", "decision": {"action": "publish", "notifications": {}}}
    summary = NotificationStore(db).fan_out("A-test")
    assert summary == {"state": "outbox_saved", "groups": 2, "jobs": 2}
    assert db.docs[source]["decision"]["notifications"] == summary and count(db, "notification_jobs") == 2


def test_worker_cycles_pass_the_verifier_and_firms_only_when_configured(repo, monkeypatch):
    from asapify_api.notification_tasks import require_worker
    seen, power = {}, {}
    monkeypatch.setattr(engine_routes, "run_geo_cycle", lambda *a, **kw: seen.update(kw) or {"ok": True})
    monkeypatch.setattr(engine_routes, "run_cycle", lambda *a, **kw: power.update(kw) or {"power": True})
    app.dependency_overrides[require_worker] = lambda: None
    assert client().post("/_internal/engine/geo-tick?budget=240").json() == {"ok": True}
    assert seen["budget_s"] == 240 and callable(seen["verify"]) and callable(seen["hotspots"])
    assert callable(seen["published"]) and callable(seen["observe"])
    monkeypatch.setattr(engine_routes, "Settings", lambda: type(SETTINGS)(project="p", firms_map_key=""))
    client().post("/_internal/engine/geo-tick")
    assert seen["verify"] is None and seen["hotspots"] is None and seen["budget_s"] == 240
    assert client().post("/_internal/engine/tick?batch=20&budget=40").json() == {"power": True}
    assert power == {"batch": 20, "budget_s": 40}                 # POWER only supports: no agent from that cycle
