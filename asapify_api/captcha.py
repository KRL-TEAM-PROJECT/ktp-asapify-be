"""reCAPTCHA Enterprise check for the operator self-registration of the judging period (access_routes.py).

The web app shows a checkbox key; the token it gets is checked here with one assessment in the same Google Cloud
project, authenticated with the Cloud Run service account (role reCAPTCHA Enterprise Agent), so no secret key is
stored. A token counts when it is valid, was made for this action and its score is at least MIN_SCORE. The check sits
behind a protocol, so the tests need no Google project.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Protocol

import httpx

from .core import ApiProblem

ACTION = "operator_signup"
MIN_SCORE = 0.5
TIMEOUT_S = 8
URL = "https://recaptchaenterprise.googleapis.com/v1/projects/{project}/assessments"


class Captcha(Protocol):
    def verify(self, token: str, site_key: str, action: str) -> bool: ...


def assessment_ok(assessment: dict, action: str) -> bool:
    """A valid token made for `action`, and a score that is absent or at least MIN_SCORE."""
    props = assessment.get("tokenProperties") or {}
    score = (assessment.get("riskAnalysis") or {}).get("score")
    return props.get("valid") is True and props.get("action") == action and (score is None or score >= MIN_SCORE)


class RecaptchaEnterprise:
    def __init__(self, project: str):
        self.project = project

    def verify(self, token: str, site_key: str, action: str) -> bool:
        import google.auth
        from google.auth.transport.requests import Request

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(Request())
        response = httpx.post(URL.format(project=self.project), timeout=TIMEOUT_S,
                              headers={"Authorization": f"Bearer {credentials.token}"},
                              json={"event": {"token": token, "siteKey": site_key, "expectedAction": action}})
        response.raise_for_status()
        return assessment_ok(response.json(), action)


@lru_cache
def get_captcha() -> Captcha:
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise ApiProblem(503, "SIGNUP_NOT_CONFIGURED", "Self-registration is not configured.")
    return RecaptchaEnterprise(project)
