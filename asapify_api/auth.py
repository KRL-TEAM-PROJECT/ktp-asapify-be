from __future__ import annotations

import os
from functools import lru_cache

import firebase_admin
from firebase_admin import auth
from fastapi import Header

from .core import ApiProblem


@lru_cache
def firebase_app():
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise ApiProblem(503, "AUTH_NOT_CONFIGURED", "The authentication project is not configured.")
    # Production never accepts unsigned emulator tokens, even if an env var was left behind.
    if os.environ.get("K_SERVICE") and os.environ.get("FIREBASE_AUTH_EMULATOR_HOST"):
        raise ApiProblem(503, "AUTH_NOT_CONFIGURED", "Invalid Cloud Run authentication configuration.")
    return firebase_admin.initialize_app(options={"projectId": project}, name="asapify-operator")


def require_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiProblem(401, "UNAUTHENTICATED", "Sign in with a Google account.")
    token = authorization[7:]
    try:
        claims = auth.verify_id_token(token, app=firebase_app(), check_revoked=True)
    except ApiProblem:
        raise
    except (auth.InvalidIdTokenError, auth.RevokedIdTokenError, auth.UserDisabledError, ValueError):
        raise ApiProblem(401, "UNAUTHENTICATED", "The session is invalid or has expired.")
    except Exception:
        # Do not put JWTs, Google response bodies, or credentials in logs/responses.
        raise ApiProblem(503, "AUTH_UNAVAILABLE", "The authentication service is not available yet.")
    if not claims.get("email_verified"):
        raise ApiProblem(403, "FORBIDDEN", "Verify your email address before subscribing.")
    return {"uid": claims["uid"], "email": claims.get("email", claims["uid"]), "role": claims.get("role")}


def require_operator(authorization: str | None = Header(default=None)) -> dict:
    actor = require_user(authorization)
    if actor.get("role") != "operator":
        raise ApiProblem(403, "FORBIDDEN", "This account does not have operator access.")
    return actor
