"""What the Telegram group reads: the operator's message, then where and when, built by the backend.

The operator writes (or edits the agent's draft of) the message; the lines below it come from the verification, so
every alert carries its coordinates, a map link and the slot whatever the caption says. The region page shows the
same lines to the operator before publishing (`telegram_details` in the region detail).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from asapify_engine.regions import PROVINCE_NAMES, Region

WIB = timezone(timedelta(hours=7), "UTC+7")
POINT = {"hot_pixel": "hot pixel", "strongest_pixel": "strongest pixel", "region_centre": "region centre"}
STATUS = {"ALERT": "Alert", "WARNING": "Warning", "SAFE": "Safe", "NO_OBSERVABLE": "Not observable"}


def public_origin() -> str:
    """The app's public address (set on both services by gcp/telegram-live.sh), or '' when unset or not https."""
    origin = os.getenv("ASAPIFY_PUBLIC_ORIGIN", "").rstrip("/")
    return origin if origin.startswith("https://") else ""


def _coord(lat: float, lon: float) -> str:
    return f"{abs(lat):.4f}° {'S' if lat < 0 else 'N'}, {abs(lon):.4f}° {'W' if lon < 0 else 'E'}"


def _slot(observed_at: str | None) -> str | None:
    try:
        when = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00")).astimezone(WIB)
    except ValueError:
        return None
    return f"{when:%d %b %Y %H:%M} UTC+7"


def _firms(verification: dict) -> str | None:
    """The FIRMS result the verdict relies on: its hotspot count, the confident ones and the search window."""
    data = verification.get("supporting_data") or {}
    count = data.get("firms_hotspot_count")
    if count is None:
        return None
    calls = [c for c in verification.get("tool_calls") or [] if not c.get("error")]
    used = next((c for c in reversed(calls) if c.get("hotspot_count") == count), None)
    if used is None or used.get("confident_count") is None:
        return f"NASA FIRMS VIIRS: {count} hotspots"
    window = f" within ±{used['hours_window']} h of the slot" if used.get("hours_window") else ""
    return f"NASA FIRMS VIIRS: {count} hotspots ({used['confident_count']} nominal or high confidence){window}"


def alert_details(region: Region, verification: dict, origin: str | None = None) -> str:
    """Plain text (Telegram gets no parse mode): region, coordinates, map link, slot and FIRMS evidence."""
    where = verification.get("location") or {}
    lat, lon = where.get("lat"), where.get("lon")
    if lat is None or lon is None:   # verifications stored before 17 Oct 2026 have no location: the region centre
        lat, lon, where = region.lat, region.lon, {"point": "region_centre"}
    status, slot = verification.get("region_status"), _slot(verification.get("observed_at"))
    lines = [
        f"Region: {region.id}, {PROVINCE_NAMES.get(region.province, region.province)}",
        f"Coordinates ({POINT.get(where.get('point'), 'region centre')}): {_coord(lat, lon)}",
        f"Map: https://www.google.com/maps?q={lat:.4f},{lon:.4f}",
    ]
    if slot:
        lines.append(f"Satellite slot: {slot}" + (f", status {STATUS.get(status, status)}" if status else ""))
    firms = _firms(verification)
    if firms:
        lines.append(firms)
    origin = public_origin() if origin is None else origin
    if origin:
        lines.append(f"Details: {origin}/regions/{region.id}")
    return "\n".join(lines)


def alert_text(caption: str, region: Region, verification: dict, origin: str | None = None) -> str:
    """The whole message: the operator's caption, a blank line, then the details."""
    return f"{caption.strip()}\n\n{alert_details(region, verification, origin)}"
