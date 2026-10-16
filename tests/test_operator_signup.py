"""Self-registration as operator for the judging period: the window, the CAPTCHA, the limits. Offline fakes only."""
import pytest
from fastapi.testclient import TestClient

from asapify_api import auth
from asapify_api.access_routes import get_access_store, get_accounts
from asapify_api.app import app
from asapify_api.auth import require_operator, require_user
from asapify_api.captcha import ACTION, assessment_ok, get_captcha
from asapify_api.core import ApiProblem
from test_operator_access import FakeAccounts, FakeStore, user

OPEN, PAST, KEY = "2099-12-31", "2020-01-01", "site-key-123"
JUDGE = {"uid": "u-judge", "email": "Judge@example.org", "role": None, "signup_until": None}
TOKEN = {"token": "t" * 40}


class FakeCaptcha:
    def __init__(self, answer=True):
        self.answer, self.calls = answer, []

    def verify(self, token, site_key, action):
        self.calls.append((token, site_key, action))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OPERATOR_SIGNUP_UNTIL", OPEN)
    monkeypatch.setenv("RECAPTCHA_SITE_KEY", KEY)
    accounts = FakeAccounts(user("operator@example.org", "u-op", role="operator"), user("judge@example.org", "u-judge"))
    store, captcha = FakeStore(), FakeCaptcha()
    app.dependency_overrides.update({get_accounts: lambda: accounts, get_access_store: lambda: store,
                                     get_captcha: lambda: captcha, require_user: lambda: JUDGE})
    yield accounts, store, captcha
    app.dependency_overrides.clear()


def client():
    return TestClient(app, raise_server_exceptions=False)


def test_the_status_is_public_and_closed_unless_a_date_and_a_key_are_set(monkeypatch):
    monkeypatch.delenv("OPERATOR_SIGNUP_UNTIL", raising=False)
    monkeypatch.setenv("RECAPTCHA_SITE_KEY", KEY)
    c = client()
    assert c.get("/api/v1/access/signup").json() == {"open": False, "until": None, "site_key": None}
    monkeypatch.setenv("OPERATOR_SIGNUP_UNTIL", PAST)
    assert c.get("/api/v1/access/signup").json()["open"] is False
    monkeypatch.setenv("OPERATOR_SIGNUP_UNTIL", "not-a-date")
    assert c.get("/api/v1/access/signup").json()["open"] is False
    monkeypatch.setenv("OPERATOR_SIGNUP_UNTIL", OPEN)
    assert c.get("/api/v1/access/signup").json() == {"open": True, "until": OPEN, "site_key": KEY}
    monkeypatch.delenv("RECAPTCHA_SITE_KEY")
    assert c.get("/api/v1/access/signup").json()["open"] is False


def test_solving_the_captcha_makes_a_self_registered_operator_until_the_window_ends(env):
    accounts, store, captcha = env
    r = client().post("/api/v1/access/signup", json=TOKEN)
    assert r.json() == {"role": "operator", "changed": True, "signup_until": OPEN}
    assert captcha.calls == [(TOKEN["token"], KEY, ACTION)]
    assert accounts.users["judge@example.org"]["claims"] == {"role": "operator", "signup_until": OPEN}
    assert store.docs["judge@example.org"]["added_by"] == "self-signup"
    assert store.audit[-1]["action"] == "operator_self_signup" and store.audit[-1]["by"] == "judge@example.org"


@pytest.mark.parametrize("answer, status, code", [(False, 403, "CAPTCHA_FAILED"),
                                                  (RuntimeError("no network"), 503, "CAPTCHA_UNAVAILABLE")])
def test_a_failed_or_unavailable_captcha_grants_nothing(env, answer, status, code):
    accounts, store, captcha = env
    captcha.answer = answer
    r = client().post("/api/v1/access/signup", json=TOKEN)
    assert r.status_code == status and r.json()["error"]["code"] == code
    assert accounts.users["judge@example.org"]["claims"] == {} and store.audit == []


def test_a_closed_window_grants_nothing_and_never_asks_google(env, monkeypatch):
    accounts, _, captcha = env
    monkeypatch.setenv("OPERATOR_SIGNUP_UNTIL", PAST)
    r = client().post("/api/v1/access/signup", json=TOKEN)
    assert r.status_code == 403 and r.json()["error"]["code"] == "SIGNUP_CLOSED" and captcha.calls == []
    assert accounts.users["judge@example.org"]["claims"] == {}


def test_signing_up_needs_a_signed_in_account_and_leaves_full_operators_alone(env):
    app.dependency_overrides.pop(require_user)
    assert client().post("/api/v1/access/signup", json=TOKEN).status_code == 401
    accounts, _, captcha = env
    app.dependency_overrides[require_user] = lambda: {"uid": "u-op", "email": "operator@example.org",
                                                      "role": "operator", "signup_until": None}
    assert client().post("/api/v1/access/signup", json=TOKEN).json()["changed"] is False and captcha.calls == []
    assert accounts.users["operator@example.org"]["claims"] == {"role": "operator"}


def test_a_self_registered_operator_cannot_use_the_admin_page(env):
    app.dependency_overrides[require_operator] = lambda: {**JUDGE, "role": "operator", "signup_until": OPEN}
    c = client()
    for method, path, body in [("get", "/api/v1/operator/access", None),
                               ("post", "/api/v1/operator/access", {"email": "x@example.org"}),
                               ("post", "/api/v1/operator/access/revoke", {"email": "operator@example.org"}),
                               ("get", "/api/v1/operator/telegram-groups", None),
                               ("post", "/api/v1/operator/telegram-groups/link", None),
                               ("delete", f"/api/v1/operator/telegram-groups/{'a' * 32}", None)]:
        r = getattr(c, method)(path, **({"json": body} if body else {}))
        assert r.status_code == 403 and r.json()["error"]["code"] == "ADMIN_ONLY", path


def test_the_access_ends_with_the_window_for_self_registered_operators_only(monkeypatch):
    signed_up = {"uid": "u-judge", "email": "judge@example.org", "role": "operator", "signup_until": OPEN}
    added = {**signed_up, "uid": "u-op", "signup_until": None}
    monkeypatch.setenv("OPERATOR_SIGNUP_UNTIL", OPEN)
    monkeypatch.setattr(auth, "require_user", lambda authorization: signed_up)
    assert auth.require_operator("Bearer x") == signed_up
    monkeypatch.setenv("OPERATOR_SIGNUP_UNTIL", PAST)
    with pytest.raises(ApiProblem) as ended:
        auth.require_operator("Bearer x")
    assert ended.value.code == "SIGNUP_ENDED"
    monkeypatch.setattr(auth, "require_user", lambda authorization: added)
    assert auth.require_operator("Bearer x") == added   # operators someone added are not affected


def test_adding_a_self_registered_account_from_admin_makes_it_a_full_operator(env):
    accounts, _, _ = env
    client().post("/api/v1/access/signup", json=TOKEN)
    app.dependency_overrides[require_operator] = lambda: {"uid": "u-op", "email": "operator@example.org",
                                                          "role": "operator", "signup_until": None}
    r = client().post("/api/v1/operator/access", json={"email": "judge@example.org"})
    assert r.status_code == 201 and accounts.users["judge@example.org"]["claims"] == {"role": "operator"}


@pytest.mark.parametrize("assessment, ok", [
    ({"tokenProperties": {"valid": True, "action": ACTION}, "riskAnalysis": {"score": 0.9}}, True),
    ({"tokenProperties": {"valid": True, "action": ACTION}}, True),                        # no score on the key
    ({"tokenProperties": {"valid": True, "action": ACTION}, "riskAnalysis": {"score": 0.1}}, False),
    ({"tokenProperties": {"valid": True, "action": "login"}, "riskAnalysis": {"score": 0.9}}, False),
    ({"tokenProperties": {"valid": False, "invalidReason": "EXPIRED", "action": ACTION}}, False),
    ({}, False),
])
def test_an_assessment_counts_only_when_valid_for_this_action_and_not_risky(assessment, ok):
    assert assessment_ok(assessment, ACTION) is ok
