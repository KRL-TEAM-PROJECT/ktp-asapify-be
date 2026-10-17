"""Operator flow per region: Re-verify, agent quota, publish/reject and notification fan-out."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from asapify_api import engine_routes
from asapify_api.app import app
from asapify_api.auth import require_operator
from asapify_api.engine_routes import get_engine_repo
from asapify_engine.regions import PROVINCE_NAMES, load_regions
from test_engine_ingest import FakeRepo
from test_notifications import count, fanout_db
from test_verifier import RECORD, SETTINGS, TOOL, YAML_OK, fake_run

REGION = load_regions()[0]
NOW = datetime(2026, 10, 10, 6, 30, tzinfo=timezone.utc)   # half an hour after RECORD's slot: well inside 12 hours


@pytest.fixture
def repo(monkeypatch):
    repo = FakeRepo()
    repo.docs[REGION.id] = {"id": REGION.id, "status": "ALERT", "latest": {**RECORD}}
    app.dependency_overrides[get_engine_repo] = lambda: repo
    app.dependency_overrides[require_operator] = lambda: {"uid": "op", "email": "operator@example.test"}
    monkeypatch.setattr(engine_routes, "Settings", lambda: SETTINGS)
    monkeypatch.setattr(engine_routes, "utc_now", lambda: NOW)
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
    repo.quota = {NOW.date(): 2}
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


def test_the_group_gets_the_caption_with_coordinates_map_link_and_slot(repo, monkeypatch):
    monkeypatch.setenv("ASAPIFY_PUBLIC_ORIGIN", "https://asapify.example/")
    c, store, vid, body = published(repo, monkeypatch)
    preview = c.get(f"/api/v1/operator/regions/{REGION.id}").json()["verifications"][0]["telegram_details"]
    assert c.post(f"/api/v1/operator/regions/{REGION.id}/decision", json=body).status_code == 201
    text = repo.alerts[f"A-{REGION.id}-{vid}"]["text"]
    assert text == f"{body['caption']}\n\n{preview}"   # the operator saw exactly what the group reads
    assert preview.splitlines() == [
        f"Region: {REGION.id}, {PROVINCE_NAMES[REGION.province]}",
        "Coordinates (hot pixel): 2.5100° S, 113.8000° E",
        "Map: https://www.google.com/maps?q=-2.5100,113.8000",
        "Satellite slot: 10 Oct 2026 13:00 UTC+7, status Alert",
        "NASA FIRMS VIIRS: 2 hotspots (1 nominal or high confidence) in the region and 10 km around it, ±24 h from the slot",
        f"Details: https://asapify.example/regions/{REGION.id}",
    ]


def test_a_verification_without_location_takes_the_hot_pixel_of_its_slot(repo, monkeypatch):
    c, store, vid, body = published(repo, monkeypatch)
    del repo.verifications[REGION.id][-1]["location"]   # stored before 17 Oct 2026
    repo.slots[REGION.id] = [{**RECORD, "inputs": {**RECORD["inputs"], "hot_lat": -2.6, "hot_lon": 113.9}}]
    preview = c.get(f"/api/v1/operator/regions/{REGION.id}").json()["verifications"][0]["telegram_details"]
    assert "Coordinates (hot pixel): 2.6000° S, 113.9000° E" in preview
    assert c.post(f"/api/v1/operator/regions/{REGION.id}/decision", json=body).status_code == 201
    assert "Coordinates (hot pixel): 2.6000° S, 113.9000° E" in repo.alerts[f"A-{REGION.id}-{vid}"]["text"]


def test_a_hotspot_count_no_firms_result_matches_is_not_sent(monkeypatch):
    from asapify_api.alert_text import alert_details
    invented = {"observed_at": "2026-10-10T06:00:00Z", "supporting_data": {"firms_hotspot_count": 9},
                "tool_calls": [{"hotspot_count": 2, "confident_count": 1, "hours_window": 24, "buffer_km": 10.0}]}
    assert "FIRMS" not in alert_details(REGION, invented, "")


def test_older_verifications_name_the_region_centre_and_no_origin_means_no_link(monkeypatch):
    from asapify_api.alert_text import alert_details
    monkeypatch.delenv("ASAPIFY_PUBLIC_ORIGIN", raising=False)
    lines = alert_details(REGION, {"observed_at": "2026-10-10T17:10:00Z", "region_status": "WARNING"}).splitlines()
    assert lines[1].startswith("Coordinates (region centre): ") and lines[-1].endswith("11 Oct 2026 00:10 UTC+7, status Warning")
    assert not any(line.startswith("Details:") for line in lines)


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


# ---------- time limits and the automatic publication (asapify_api/publication.py, auto_publish.py) ----------

DECISION_URL = f"/api/v1/operator/regions/{REGION.id}/decision"


def test_a_verification_12_hours_past_its_slot_cannot_be_published_but_can_be_rejected(repo, monkeypatch):
    c, store, vid, body = published(repo, monkeypatch)
    monkeypatch.setattr(engine_routes, "utc_now", lambda: datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc))
    r = c.post(DECISION_URL, json=body)   # the slot was 06:00 UTC: 12 hours later the group would read old news
    assert r.status_code == 409 and r.json()["error"]["code"] == "VERIFICATION_STALE"
    reject = {"action": "reject", "reason": "too old; verified again", "verification_id": vid}
    assert c.post(DECISION_URL, json=reject).status_code == 201 and not repo.alerts


def test_the_region_detail_says_until_when_and_when_it_goes_out_by_itself(repo, monkeypatch):
    c, store, vid, body = published(repo, monkeypatch)
    v = c.get(f"/api/v1/operator/regions/{REGION.id}").json()["verifications"][0]
    assert v["publish_until"] == "2026-10-10T18:00:00Z" and v["auto_publish_at"] == "2026-10-10T08:30:00Z"
    repo.verifications[REGION.id][-1]["validation_status"] = "NEEDS_MANUAL_REVIEW"
    v = c.get(f"/api/v1/operator/regions/{REGION.id}").json()["verifications"][0]
    assert v["publish_until"] == "2026-10-10T18:00:00Z" and v["auto_publish_at"] is None   # only CONFIRMED goes out


def run_auto(repo, monkeypatch, at):
    from asapify_api.auto_publish import auto_publish
    store = Mock()
    store.fan_out.return_value = {"state": "outbox_saved", "groups": 1, "jobs": 1}
    monkeypatch.setattr(engine_routes, "get_notification_store", lambda: store)
    monkeypatch.setattr(engine_routes, "get_tasks", lambda: object())
    monkeypatch.setattr(engine_routes, "enqueue_pending", lambda store, tasks: 1)
    return auto_publish(repo, at), store


def verified(repo, status="CONFIRMED"):
    vid = client().post(f"/api/v1/operator/regions/{REGION.id}/verify").json()["id"]
    repo.verifications[REGION.id][-1]["validation_status"] = status
    repo.docs[REGION.id]["verification"]["validation_status"] = status
    return vid


def test_a_confirmed_verification_nobody_decided_goes_out_after_two_hours_marked_automatic(repo, monkeypatch):
    monkeypatch.setenv("ASAPIFY_PUBLIC_ORIGIN", "https://asapify.example")
    vid = verified(repo)
    assert run_auto(repo, monkeypatch, NOW + timedelta(hours=1, minutes=59))[0] == []   # the operator's two hours
    sent, store = run_auto(repo, monkeypatch, NOW + timedelta(hours=2))
    alert_id = f"A-{REGION.id}-{vid}"
    assert sent == [{"region_id": REGION.id, "verification_id": vid, "alert_id": alert_id}]
    alert = repo.alerts[alert_id]
    assert alert["auto"] is True and alert["published_by"] == "automatic"
    assert alert["text"].startswith(
        "[Automatic] Not yet reviewed by an ASAPify operator.\n\nThermal indication in Central Kalimantan\n\n"
        "FIRMS shows hotspots near the region. This is a thermal indication; verify it in the field.\n\nRegion: ")
    assert alert["text"].endswith(f"Details: https://asapify.example/regions/{REGION.id}")
    decision = repo.verifications[REGION.id][-1]["decision"]
    assert decision["auto"] is True and decision["by"] == "automatic" and repo.audit[-1]["operator_uid"] == "asapify-auto"
    store.fan_out.assert_called_once_with(alert_id)
    assert run_auto(repo, monkeypatch, NOW + timedelta(hours=3))[0] == []   # once
    assert client().post(DECISION_URL, json={"action": "reject", "reason": "late", "verification_id": vid}).json()[
        "error"]["code"] == "ALREADY_DECIDED"


@pytest.mark.parametrize("status", ["NEEDS_MANUAL_REVIEW", "REJECTED"])
def test_only_a_confirmed_verification_goes_out_by_itself(repo, monkeypatch, status):
    verified(repo, status)
    assert run_auto(repo, monkeypatch, NOW + timedelta(hours=3))[0] == [] and not repo.alerts


def test_nothing_goes_out_by_itself_once_the_region_left_alert_or_an_operator_decided(repo, monkeypatch):
    vid = verified(repo)
    repo.docs[REGION.id]["status"] = "WARNING"
    assert run_auto(repo, monkeypatch, NOW + timedelta(hours=3))[0] == []
    with pytest.raises(engine_routes.ApiProblem) as refused:   # checked again inside the decision transaction
        engine_routes.decide_verification(repo, REGION, vid, "publish", caption=None, reason=None,
                                          actor={"uid": "asapify-auto", "email": "automatic"},
                                          now=NOW + timedelta(hours=3), auto=True)
    assert refused.value.code == "NOT_DUE" and not repo.alerts
    repo.docs[REGION.id]["status"] = "ALERT"
    assert client().post(DECISION_URL, json={"action": "reject", "reason": "smoke from a known burn",
                                             "verification_id": vid}).status_code == 201
    assert run_auto(repo, monkeypatch, NOW + timedelta(hours=3))[0] == [] and not repo.alerts


def test_nothing_goes_out_by_itself_past_12_hours_from_its_slot(repo, monkeypatch):
    verified(repo)
    assert run_auto(repo, monkeypatch, NOW + timedelta(hours=11, minutes=30))[0] == [] and not repo.alerts


def test_the_minute_sweep_publishes_what_is_due_and_recovers_without_engine_data(repo, monkeypatch):
    from asapify_api import notification_routes
    from asapify_api.notification_routes import engine_repo_or_none
    from asapify_api.notification_store import get_notification_store
    from asapify_api.notification_tasks import get_tasks, require_worker
    vid = verified(repo)
    store = Mock()
    store.resume_fanouts.return_value = 1
    store.pending.return_value = []
    store.fan_out.return_value = {"state": "outbox_saved", "groups": 1, "jobs": 1}
    monkeypatch.setattr(engine_routes, "get_notification_store", lambda: store)
    monkeypatch.setattr(engine_routes, "get_tasks", lambda: object())
    monkeypatch.setattr(notification_routes, "utc_now", lambda: NOW + timedelta(hours=2, minutes=1))
    app.dependency_overrides.update({require_worker: lambda: None, get_notification_store: lambda: store,
                                     get_tasks: lambda: object(), engine_repo_or_none: lambda: repo})
    assert client().post("/_internal/notifications/sweep").json() == {"auto_published": 1, "resumed": 1, "enqueued": 0}
    assert f"A-{REGION.id}-{vid}" in repo.alerts
    app.dependency_overrides[engine_repo_or_none] = lambda: None   # no engine data: the outbox is still recovered
    assert client().post("/_internal/notifications/sweep").json() == {"auto_published": None, "resumed": 1, "enqueued": 0}
