"""When a verification may be published, and when it goes out by itself.

Operators decide. Two time limits keep that honest (team decision of 18 October 2026):

- A verification can be published until STALE_AFTER (12 hours) after the slot it reviewed. Past that the group would
  read old news as a warning; an operator verifies again and publishes the new result. Rejecting stays possible.
- A safety net for when nobody is on duty: a verification Gemini CONFIRMED that no operator decided on within
  AUTO_AFTER (2 hours) is published by itself while its region is still ALERT, with Gemini's draft under a label that
  says no operator reviewed it (auto_publish.py). NEEDS_MANUAL_REVIEW and REJECTED are never sent by themselves.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from asapify_engine.regions import PROVINCE_NAMES, Region

STALE_AFTER = timedelta(hours=12)
AUTO_AFTER = timedelta(hours=2)
AUTO_ACTOR = {"uid": "asapify-auto", "email": "automatic"}
AUTO_LABEL = "[Automatic] Not yet reviewed by an ASAPify operator."
CAPTION_MAX = 1024   # UTF-16 code units, as core.DecisionInput checks an operator's caption


def _time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def publish_until(verification: dict) -> datetime | None:
    """The last moment the verification can be published: STALE_AFTER after its slot (its run when that is unknown)."""
    start = _time(verification.get("observed_at")) or _time(verification.get("at"))
    return start + STALE_AFTER if start else None


def auto_publish_at(verification: dict) -> datetime | None:
    """When an undecided CONFIRMED verification goes out by itself (if its region is still ALERT then), else None."""
    if verification.get("validation_status") != "CONFIRMED" or verification.get("decision") or verification.get("error"):
        return None
    at, until = _time(verification.get("at")), publish_until(verification)
    if at is None:
        return None
    when = at + AUTO_AFTER
    return when if until is None or when < until else None


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def auto_caption(region: Region, verification: dict) -> str:
    """The label, then Gemini's draft (or a plain default when it wrote none), within the caption limit."""
    draft = verification.get("draft_operator_alert") or {}
    body = "\n\n".join(s.strip() for s in (draft.get("title"), draft.get("message")) if isinstance(s, str) and s.strip())
    body = body or (f"Thermal indication near peat region {region.id} "
                    f"({PROVINCE_NAMES.get(region.province, region.province)}). Verify it in the field before acting.")
    room = CAPTION_MAX - _units(AUTO_LABEL) - 2
    if _units(body) > room:
        while _units(body) > room - 1:
            body = body[:-1]
        body = body.rstrip() + "…"
    return f"{AUTO_LABEL}\n\n{body}"
