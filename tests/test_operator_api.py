"""Health and operator authentication, shared by every /api/v1/operator endpoint."""
import pytest
from fastapi.testclient import TestClient

from asapify_api import auth
from asapify_api.app import app
from asapify_api.engine_routes import get_engine_repo


class NoStates:
    def get_states(self):
        return {}


@pytest.fixture(autouse=True)
def reset():
    app.dependency_overrides.clear()
    app.dependency_overrides[get_engine_repo] = NoStates
    yield
    app.dependency_overrides.clear()


def client():
    return TestClient(app, raise_server_exceptions=False)


def test_health_without_credentials():
    r = client().get("/api/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


@pytest.mark.parametrize("path", ["regions", "days?month=2026-10", "agent", "access"])
def test_operator_reads_require_auth_before_data(path):
    r = client().get("/api/v1/operator/" + path)
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"


def test_invalid_token(monkeypatch):
    monkeypatch.setattr(auth, "firebase_app", lambda: None)
    def invalid(*a, **kw):
        raise ValueError("invalid JWT")
    monkeypatch.setattr(auth.auth, "verify_id_token", invalid)
    r = client().get("/api/v1/operator/regions", headers={"Authorization": "Bearer forged"})
    assert r.status_code == 401


def test_signed_user_without_role_is_forbidden(monkeypatch):
    monkeypatch.setattr(auth, "firebase_app", lambda: None)
    monkeypatch.setattr(auth.auth, "verify_id_token", lambda *a, **kw: {"uid": "a", "email_verified": True})
    assert client().get("/api/v1/operator/regions", headers={"Authorization": "Bearer valid"}).status_code == 403


def test_revocation_check_is_requested(monkeypatch):
    monkeypatch.setattr(auth, "firebase_app", lambda: None)
    seen = {}
    def verify(*a, **kw):
        seen.update(kw)
        return {"uid": "test", "role": "operator", "email_verified": True}
    monkeypatch.setattr(auth.auth, "verify_id_token", verify)
    r = client().get("/api/v1/operator/regions", headers={"Authorization": "Bearer valid"})
    assert r.status_code == 200 and seen["check_revoked"] is True


@pytest.mark.parametrize("path", ["meta", "operator/pixels", "operator/grid", "operator/nights?month=2026-10",
                                  "operator/clusters"])
def test_old_pixel_api_is_gone(path):
    assert client().get("/api/v1/" + path).status_code == 404


def test_alert_list_and_close_stubs_are_gone():
    assert client().get("/api/v1/operator/alerts").status_code == 404
    assert client().post("/api/v1/operator/alerts/A-1/close").status_code == 404
