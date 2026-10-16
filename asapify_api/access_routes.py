"""Operator access from the app: an allowlist of email addresses that become operators.

Any operator can add or remove an address. An address whose Google account already exists gets the
`role=operator` claim at once; any other waits in `operator_access/{email}` and gets it at its first sign-in, when the
web app calls POST /api/v1/access/activate. Removing an address drops the claim and revokes the account's sessions,
so the access ends with the next request. Every change lands in `audit_log`. Addresses travel in request bodies,
never in URLs, so they stay out of request logs. tools/set_operator.py remains for the very first operator.

For the hackathon judging period only, a signed-in account can also register itself as an operator after a
reCAPTCHA Enterprise check (POST /api/v1/access/signup), while OPERATOR_SIGNUP_UNTIL is set and not past. Such an
account gets `signup_until` next to `role=operator`: it may verify and decide, but not use the Admin page, and its
access ends with the window (auth.require_operator). Adding it from the Admin page makes it a full operator.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Protocol

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .auth import firebase_app, require_admin, require_user, signup_open, signup_until
from .captcha import ACTION, get_captcha
from .core import ApiProblem, iso, utc_now

EMAIL = re.compile(r"^[^@\s/]+@[^@\s/]+\.[^@\s/]+$")
router = APIRouter()


class Accounts(Protocol):
    """Firebase Authentication behind a protocol, so the tests need no Google project."""
    def find(self, email: str) -> dict | None: ...           # {uid, email, email_verified, claims}; None = no account
    def set_role(self, uid: str, role: str | None, signup_until: str | None = None) -> None: ...   # other claims kept
    def revoke(self, uid: str) -> None: ...                   # ends every session of the account
    def operators(self) -> list[dict]: ...                    # accounts holding role=operator: {uid, email}


class FirebaseAccounts:
    def find(self, email: str) -> dict | None:
        from firebase_admin import auth
        try:
            user = auth.get_user_by_email(email, app=firebase_app())
        except auth.UserNotFoundError:
            return None
        return {"uid": user.uid, "email": (user.email or email).lower(), "email_verified": user.email_verified,
                "claims": dict(user.custom_claims or {})}

    def set_role(self, uid: str, role: str | None, signup_until: str | None = None) -> None:
        from firebase_admin import auth
        claims = dict(auth.get_user(uid, app=firebase_app()).custom_claims or {})
        claims.pop("signup_until", None)   # an operator someone added, or none at all: no longer self-registered
        if role:
            claims["role"] = role
        else:
            claims.pop("role", None)
        if role and signup_until:
            claims["signup_until"] = signup_until
        auth.set_custom_user_claims(uid, claims or None, app=firebase_app())

    def revoke(self, uid: str) -> None:
        from firebase_admin import auth
        auth.revoke_refresh_tokens(uid, app=firebase_app())

    def operators(self) -> list[dict]:
        from firebase_admin import auth
        return [{"uid": u.uid, "email": (u.email or u.uid).lower()}
                for u in auth.list_users(app=firebase_app()).iterate_all()
                if (u.custom_claims or {}).get("role") == "operator"]


class AccessStore:
    """operator_access/{email}: who added the address, when, and when its account became an operator."""
    def __init__(self, db):
        self.db = db
        self.entries_ref = db.collection("operator_access")

    def entries(self) -> dict[str, dict]:
        return {d.id: d.to_dict() for d in self.entries_ref.stream()}

    def get(self, email: str) -> dict | None:
        snap = self.entries_ref.document(email).get()
        return snap.to_dict() if snap.exists else None

    def save(self, email: str, entry: dict, audit: dict) -> None:
        batch = self.db.batch()
        batch.set(self.entries_ref.document(email), entry)
        batch.set(self.db.collection("audit_log").document(), audit)
        batch.commit()

    def remove(self, email: str, audit: dict) -> None:
        batch = self.db.batch()
        batch.delete(self.entries_ref.document(email))
        batch.set(self.db.collection("audit_log").document(), audit)
        batch.commit()


@lru_cache
def get_access_store() -> AccessStore:
    from google.cloud import firestore
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise ApiProblem(503, "DATA_NOT_READY", "Data storage is not configured.")
    return AccessStore(firestore.Client(project=project, database=os.getenv("FIRESTORE_DATABASE", "asapify")))


@lru_cache
def get_accounts() -> Accounts:
    return FirebaseAccounts()


class AccessRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


def normalise(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL.match(email):
        raise ApiProblem(400, "VALIDATION", "Enter one email address.")
    return email


@router.get("/api/v1/operator/access", dependencies=[Depends(require_admin)])
def access(store=Depends(get_access_store), accounts=Depends(get_accounts)):
    """Everyone with operator access, plus the addresses still waiting for their first sign-in."""
    listed = store.entries()
    active = {a["email"] for a in accounts.operators()}
    return [{"email": email, "state": "active" if email in active else "pending",
             **{k: (listed.get(email) or {}).get(k) for k in ("added_by", "added_at", "activated_at", "signup_until")}}
            for email in sorted(set(listed) | active)]


@router.post("/api/v1/operator/access", status_code=201)
def grant(body: AccessRequest, actor=Depends(require_admin), store=Depends(get_access_store),
          accounts=Depends(get_accounts)):
    """Add an address: an existing, verified Google account becomes an operator now, any other at its first sign-in.
    A self-registered account becomes a full operator."""
    email, now = normalise(body.email), iso(utc_now())
    account = accounts.find(email)
    active = bool(account and account["email_verified"])
    if active and (account["claims"].get("role") != "operator" or account["claims"].get("signup_until")):
        accounts.set_role(account["uid"], "operator")
    entry = {"email": email, "added_by": actor["email"], "added_at": now,
             "uid": account["uid"] if account else None, "activated_at": now if active else None}
    state = "active" if active else "pending"
    store.save(email, entry, {"action": "operator_grant", "email": email, "operator_uid": actor["uid"],
                              "by": actor["email"], "at": now, "state": state})
    return {"email": email, "state": state, "added_by": actor["email"], "added_at": now,
            "activated_at": entry["activated_at"]}


@router.post("/api/v1/operator/access/revoke")
def revoke(body: AccessRequest, actor=Depends(require_admin), store=Depends(get_access_store),
           accounts=Depends(get_accounts)):
    """Remove an address: the claim goes, the account's sessions end and the allowlist entry is deleted."""
    email = normalise(body.email)
    if email == actor["email"].lower():
        raise ApiProblem(409, "CANNOT_REMOVE_SELF", "You cannot remove your own operator access.")
    account = accounts.find(email)
    is_operator = bool(account and account["claims"].get("role") == "operator")
    if not is_operator and store.get(email) is None:
        raise ApiProblem(404, "NOT_FOUND", "This address has no operator access.")
    if is_operator:
        accounts.set_role(account["uid"], None)
        accounts.revoke(account["uid"])
    store.remove(email, {"action": "operator_revoke", "email": email, "operator_uid": actor["uid"],
                         "by": actor["email"], "at": iso(utc_now())})
    return {"email": email, "state": "removed"}


@router.post("/api/v1/access/activate")
def activate(actor=Depends(require_user), store=Depends(get_access_store), accounts=Depends(get_accounts)):
    """Called by the web app after sign-in. An allowlisted address becomes an operator; then refresh the ID token."""
    if actor.get("role") == "operator":
        return {"role": "operator", "changed": False}
    email = actor["email"].lower()
    entry = store.get(email)
    if entry is None:
        return {"role": actor.get("role"), "changed": False}
    accounts.set_role(actor["uid"], "operator")
    now = iso(utc_now())
    store.save(email, {**entry, "uid": actor["uid"], "activated_at": now},
               {"action": "operator_activate", "email": email, "operator_uid": actor["uid"], "by": email, "at": now})
    return {"role": "operator", "changed": True}


# --- self-registration for the judging period --------------------------------------------------------------------
SELF = "self-signup"


class SignupRequest(BaseModel):
    token: str = Field(min_length=20, max_length=4096)   # the reCAPTCHA Enterprise token from the checkbox


def _site_key() -> str | None:
    return os.getenv("RECAPTCHA_SITE_KEY", "").strip() or None


@router.get("/api/v1/access/signup")
def signup_status():
    """Public: whether self-registration is open, its last day (WIB) and the reCAPTCHA site key the app shows."""
    until, key = signup_until(), _site_key()
    is_open = bool(key) and signup_open()
    return {"open": is_open, "until": until.isoformat() if is_open else None, "site_key": key if is_open else None}


@router.post("/api/v1/access/signup")
def signup(body: SignupRequest, actor=Depends(require_user), store=Depends(get_access_store),
           accounts=Depends(get_accounts), captcha=Depends(get_captcha)):
    """A signed-in account that solved the CAPTCHA becomes a self-registered operator until the window closes;
    then refresh the ID token. An operator someone added stays as they are."""
    if actor.get("role") == "operator" and not actor.get("signup_until"):
        return {"role": "operator", "changed": False, "signup_until": None}
    until, key = signup_until(), _site_key()
    if not key or not signup_open():
        raise ApiProblem(403, "SIGNUP_CLOSED", "Self-registration is closed.")
    try:
        passed = captcha.verify(body.token, key, ACTION)
    except Exception:   # Google unreachable or the service account lacks the role: never pass the details on
        raise ApiProblem(503, "CAPTCHA_UNAVAILABLE", "The CAPTCHA check is not available. Try again later.")
    if not passed:
        raise ApiProblem(403, "CAPTCHA_FAILED", "The CAPTCHA check failed. Try again.")
    email, now, last = actor["email"].lower(), iso(utc_now()), until.isoformat()
    accounts.set_role(actor["uid"], "operator", signup_until=last)
    store.save(email, {"email": email, "added_by": SELF, "added_at": now, "uid": actor["uid"], "activated_at": now,
                       "signup_until": last},
               {"action": "operator_self_signup", "email": email, "operator_uid": actor["uid"], "by": email,
                "at": now, "signup_until": last})
    return {"role": "operator", "changed": True, "signup_until": last}
