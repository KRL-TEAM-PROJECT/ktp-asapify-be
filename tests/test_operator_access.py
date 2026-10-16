"""Operator access from the app: allowlist, activation at sign-in, removal. Offline: fake accounts and store."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from asapify_api import access_routes
from asapify_api.access_routes import get_access_store, get_accounts
from asapify_api.app import app
from asapify_api.auth import require_operator, require_user

OPERATOR = {"uid": "u-op", "email": "operator@example.org", "role": "operator"}


class FakeAccounts:
    def __init__(self, *users):
        self.users = {u["email"]: dict(u) for u in users}
        self.revoked = []

    def find(self, email):
        user = self.users.get(email)
        return None if user is None else {**user, "claims": dict(user["claims"])}

    def set_role(self, uid, role, signup_until=None):   # like FirebaseAccounts: other claims are kept
        user = next(u for u in self.users.values() if u["uid"] == uid)
        user["claims"] = ({k: v for k, v in user["claims"].items() if k not in ("role", "signup_until")}
                          | ({"role": role} if role else {}) | ({"signup_until": signup_until} if role and signup_until else {}))

    def revoke(self, uid):
        self.revoked.append(uid)

    def operators(self):
        return [{"uid": u["uid"], "email": e} for e, u in self.users.items() if u["claims"].get("role") == "operator"]


class FakeStore:
    def __init__(self):
        self.docs, self.audit = {}, []

    def entries(self):
        return dict(self.docs)

    def get(self, email):
        return self.docs.get(email)

    def save(self, email, entry, audit):
        self.docs[email] = entry
        self.audit.append(audit)

    def remove(self, email, audit):
        self.docs.pop(email, None)
        self.audit.append(audit)


def user(email, uid, verified=True, **claims):
    return {"uid": uid, "email": email, "email_verified": verified, "claims": claims}


@pytest.fixture
def env():
    accounts = FakeAccounts(user("operator@example.org", "u-op", role="operator"),
                            user("teammate@example.org", "u-team", team="be"))
    store = FakeStore()
    app.dependency_overrides[get_accounts] = lambda: accounts
    app.dependency_overrides[get_access_store] = lambda: store
    app.dependency_overrides[require_operator] = lambda: OPERATOR
    yield accounts, store
    app.dependency_overrides.clear()


def client():
    return TestClient(app, raise_server_exceptions=False)


def test_access_needs_a_signed_in_operator():
    c = client()
    assert c.get("/api/v1/operator/access").status_code == 401
    assert c.post("/api/v1/operator/access", json={"email": "a@example.org"}).status_code == 401
    assert c.post("/api/v1/access/activate").status_code == 401


def test_an_existing_account_becomes_operator_now_and_a_new_address_waits(env):
    accounts, store = env
    c = client()
    now = c.post("/api/v1/operator/access", json={"email": "  Teammate@Example.org "})
    assert now.status_code == 201 and now.json()["state"] == "active"
    assert accounts.users["teammate@example.org"]["claims"] == {"team": "be", "role": "operator"}   # other claims kept
    later = c.post("/api/v1/operator/access", json={"email": "judge@example.org"}).json()
    assert later["state"] == "pending" and later["activated_at"] is None and later["added_by"] == "operator@example.org"
    assert [a["state"] for a in store.audit] == ["active", "pending"] and store.audit[0]["action"] == "operator_grant"
    listing = c.get("/api/v1/operator/access").json()
    assert [(x["email"], x["state"]) for x in listing] == [
        ("judge@example.org", "pending"), ("operator@example.org", "active"), ("teammate@example.org", "active")]
    assert listing[1]["added_by"] is None                                   # granted earlier with tools/set_operator.py


def test_an_unverified_account_waits_until_it_signs_in_verified(env):
    accounts, _ = env
    accounts.users["new@example.org"] = user("new@example.org", "u-new", verified=False)
    r = client().post("/api/v1/operator/access", json={"email": "new@example.org"}).json()
    assert r["state"] == "pending" and accounts.users["new@example.org"]["claims"] == {}


def test_an_allowlisted_address_becomes_operator_at_sign_in(env):
    accounts, store = env
    client().post("/api/v1/operator/access", json={"email": "judge@example.org"})
    accounts.users["judge@example.org"] = user("judge@example.org", "u-judge")   # signs in with Google for the first time
    app.dependency_overrides[require_user] = lambda: {"uid": "u-judge", "email": "Judge@example.org", "role": None}
    c = client()
    assert c.post("/api/v1/access/activate").json() == {"role": "operator", "changed": True}
    assert accounts.users["judge@example.org"]["claims"] == {"role": "operator"}
    assert store.docs["judge@example.org"]["uid"] == "u-judge" and store.docs["judge@example.org"]["activated_at"]
    assert store.audit[-1]["action"] == "operator_activate"
    app.dependency_overrides[require_user] = lambda: {"uid": "u-x", "email": "someone@example.org", "role": None}
    assert c.post("/api/v1/access/activate").json() == {"role": None, "changed": False}
    app.dependency_overrides[require_user] = lambda: OPERATOR
    assert c.post("/api/v1/access/activate").json() == {"role": "operator", "changed": False}


def test_removal_drops_the_claim_and_the_sessions_but_never_your_own(env):
    accounts, store = env
    c = client()
    c.post("/api/v1/operator/access", json={"email": "teammate@example.org"})
    c.post("/api/v1/operator/access", json={"email": "judge@example.org"})
    gone = c.post("/api/v1/operator/access/revoke", json={"email": "teammate@example.org"})
    assert gone.json() == {"email": "teammate@example.org", "state": "removed"}
    assert accounts.users["teammate@example.org"]["claims"] == {"team": "be"} and accounts.revoked == ["u-team"]
    assert "teammate@example.org" not in store.docs and store.audit[-1]["action"] == "operator_revoke"
    assert c.post("/api/v1/operator/access/revoke", json={"email": "judge@example.org"}).status_code == 200
    assert accounts.revoked == ["u-team"]                                   # a pending address has no session to end
    own = c.post("/api/v1/operator/access/revoke", json={"email": "Operator@example.org"})
    assert own.status_code == 409 and own.json()["error"]["code"] == "CANNOT_REMOVE_SELF"
    assert c.post("/api/v1/operator/access/revoke", json={"email": "nobody@example.org"}).status_code == 404


@pytest.mark.parametrize("email", ["not-an-email", "a b@example.org", "a@example.org/../x", "a@b"])
def test_only_one_plain_address_is_accepted(env, email):
    r = client().post("/api/v1/operator/access", json={"email": email})
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION"


def test_firebase_accounts_keep_every_other_claim(monkeypatch):
    from firebase_admin import auth as firebase_auth
    saved = {}
    monkeypatch.setattr(access_routes, "firebase_app", lambda: None)
    monkeypatch.setattr(firebase_auth, "get_user", lambda uid, app=None: SimpleNamespace(custom_claims={"role": "operator", "team": "be"}))
    monkeypatch.setattr(firebase_auth, "set_custom_user_claims", lambda uid, claims, app=None: saved.update({uid: claims}))
    access_routes.FirebaseAccounts().set_role("u1", None)
    assert saved == {"u1": {"team": "be"}}
    access_routes.FirebaseAccounts().set_role("u1", "operator")
    assert saved == {"u1": {"role": "operator", "team": "be"}}
    access_routes.FirebaseAccounts().set_role("u1", "operator", signup_until="2026-10-31")
    assert saved == {"u1": {"role": "operator", "team": "be", "signup_until": "2026-10-31"}}
