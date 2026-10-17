"""The safety net: an alert nobody decided on still reaches the Telegram group (rules in publication.py).

The notification sweep calls auto_publish every minute. It reads only the regions that are ALERT now, and publishes
the latest verification of each when Gemini CONFIRMED it, no operator decided on it within AUTO_AFTER (2 hours) and it
is not past STALE_AFTER (12 hours after its slot). The message is Gemini's draft under a label saying no operator
reviewed it. The decision transaction checks all of this again, so an operator who decides first wins, and two sweeps
running at once publish once.
"""
from __future__ import annotations

from datetime import datetime

from asapify_engine.regions import load_regions

from . import engine_routes
from .core import ApiProblem
from .publication import AUTO_ACTOR, auto_publish_at


def auto_publish(repo, now: datetime) -> list[dict]:
    regions = {r.id: r for r in load_regions()}
    sent = []
    for doc in repo.alert_regions():
        region, latest = regions.get(doc.get("id")), doc.get("verification") or {}
        when = auto_publish_at(latest)
        if region is None or when is None or now < when or not latest.get("id"):
            continue
        try:
            result = engine_routes.decide_verification(repo, region, latest["id"], "publish", caption=None, reason=None,
                                                       actor=AUTO_ACTOR, now=now, auto=True)
        except (ApiProblem, LookupError):   # decided meanwhile, no longer due or gone: nothing to send
            continue
        sent.append({"region_id": region.id, "verification_id": latest["id"], "alert_id": result["alert_id"]})
    return sent
