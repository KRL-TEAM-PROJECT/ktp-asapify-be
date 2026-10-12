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
        raise ApiProblem(503, "AUTH_NOT_CONFIGURED", "Project autentikasi belum dikonfigurasi.")
    # Production never accepts unsigned emulator tokens, even if an env var was left behind.
    if os.environ.get("K_SERVICE") and os.environ.get("FIREBASE_AUTH_EMULATOR_HOST"):
        raise ApiProblem(503, "AUTH_NOT_CONFIGURED", "Konfigurasi autentikasi Cloud Run tidak sah.")
    return firebase_admin.initialize_app(options={"projectId": project}, name="asapify-operator")


def require_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiProblem(401, "UNAUTHENTICATED", "Silakan masuk dengan akun Google.")
    token = authorization[7:]
    try:
        claims = auth.verify_id_token(token, app=firebase_app(), check_revoked=True)
    except ApiProblem:
        raise
    except (auth.InvalidIdTokenError, auth.RevokedIdTokenError, auth.UserDisabledError, ValueError):
        raise ApiProblem(401, "UNAUTHENTICATED", "Sesi tidak sah atau sudah berakhir.")
    except Exception:
        # Do not put JWTs, Google response bodies, or credentials in logs/responses.
        raise ApiProblem(503, "AUTH_UNAVAILABLE", "Layanan autentikasi belum tersedia.")
    if not claims.get("email_verified"):
        raise ApiProblem(403, "FORBIDDEN", "Verifikasi alamat email sebelum berlangganan.")
    return {"uid": claims["uid"], "email": claims.get("email", claims["uid"]), "role": claims.get("role")}


def require_operator(authorization: str | None = Header(default=None)) -> dict:
    actor = require_user(authorization)
    if actor.get("role") != "operator":
        raise ApiProblem(403, "FORBIDDEN", "Akun ini belum memiliki akses operator.")
    return actor
