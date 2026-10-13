from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

UTC = timezone.utc
# Compact status letters are part of the pipeline contract; "A" (alarm) is the WARNING level.
STATUS = {"S": "SAFE", "N": "NO_OBSERVATION", "W": "WATCH", "A": "WARNING"}
PROVINCES = {11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 61, 62, 63, 64, 65}


class ApiProblem(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def as_time(value: str | None) -> datetime:
    if value is None:
        return utc_now()
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
            raise ValueError
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApiProblem(400, "VALIDATION", "Time must be ISO UTC, e.g. 2026-10-08T13:00:00Z.")


def date_key(value: str) -> str:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        raise ApiProblem(400, "VALIDATION", "Date must be a valid YYYY-MM-DD.")


def night_of(at: datetime) -> str:
    # Matches the FE: 54 slots, 20:00–04:50 UTC+7 (13:00–21:50 UTC).
    start = at.replace(hour=13, minute=0, second=0, microsecond=0)
    return (start if at >= start else start - timedelta(days=1)).date().isoformat()


def night_slots(night: str) -> list[str]:
    start = as_time(date_key(night) + "T13:00:00Z")
    return [iso(start + timedelta(minutes=10 * i)) for i in range(54)]


def validate_grid(index: dict, grid: dict) -> bytes:
    """Refuse corrupted/misaligned payloads rather than showing incorrect safe pixels."""
    try:
        n = len(index["rows"])
        assert n > 0 and len(index["cols"]) == len(index["province"]) == n
        assert index["step"] == 0.02 and index["origin"] == [94.9, 6.2]
        assert all(isinstance(v, int) and v >= 0 for v in index["rows"] + index["cols"])
        assert all(p in PROVINCES for p in index["province"])
        assert grid["pixels_version"] == index["version"]
        assert len(grid["status"]) == n and set(grid["status"]) <= STATUS.keys()
        assert grid["n_sat"] in (0, 1, 2)
        values = base64.b64decode(grid["utility"], validate=True)
        assert len(values) == n and all(v <= 250 or v == 255 for v in values)
        assert all((s == "N") == (v == 255) for s, v in zip(grid["status"], values))
        assert grid["n_sat"] == 2 or "A" not in grid["status"]
        occupied = set()
        for cells in grid["clusters"].values():
            assert cells
            for i in cells:
                assert isinstance(i, int) and 0 <= i < n and grid["status"][i] == "A"
                assert i not in occupied
                occupied.add(i)
        as_time(grid["slot"])
        return values
    except (KeyError, TypeError, AssertionError, ValueError, binascii.Error, ApiProblem):
        raise ApiProblem(503, "DATA_INVALID", "Grid data is inconsistent. Contact the pipeline maintainers.")


def geojson(index: dict, grid: dict, province: str | None = None) -> dict:
    values = validate_grid(index, grid)
    pixel_sats = grid.get("pixel_n_sat")
    if not isinstance(pixel_sats, list) or len(pixel_sats) != len(values) or any(v not in (0, 1, 2) for v in pixel_sats):
        raise ApiProblem(503, "DATA_NOT_READY", "Per-pixel satellite counts are not available for GeoJSON yet.")
    membership = {i: cid for cid, cells in grid["clusters"].items() for i in cells}
    features = []
    for i, status in enumerate(grid["status"]):
        if province and str(index["province"][i]) != province:
            continue
        row, col = index["rows"][i], index["cols"][i]
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [
            index["origin"][0] + (col + 0.5) * index["step"],
            index["origin"][1] - (row + 0.5) * index["step"],
        ]}, "properties": {"pixel_id": f"p{row}_{col}", "status": STATUS[status],
            "utility": None if values[i] == 255 else values[i] / 250,
            "n_sat": pixel_sats[i],
            "cluster_id": membership.get(i), "province": str(index["province"][i])}})
    return {"type": "FeatureCollection", "features": features}


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["publish", "reject"]
    caption: str | None = Field(default=None, max_length=4096)
    reason: str | None = Field(default=None, max_length=2000)

    def validate_action(self) -> None:
        if self.action == "publish" and (not self.caption or not self.caption.strip()
                                        or len(self.caption.encode("utf-16-le")) // 2 > 1024):
            raise ApiProblem(422, "CAPTION_INVALID", "A caption is required, at most 1,024 characters.")
        if self.action == "reject" and (not self.reason or not self.reason.strip()):
            raise ApiProblem(422, "REASON_REQUIRED", "A rejection reason is required.")


def check_decidable(cluster: dict, request: DecisionInput) -> None:
    request.validate_action()
    if cluster.get("decision"):
        raise ApiProblem(409, "ALREADY_DECIDED", "An operator has already decided on this cluster.")
    if cluster.get("state") != "active":
        raise ApiProblem(409, "ALREADY_CLOSED", "This cluster is already closed.")
    if request.action == "publish" and not cluster.get("verification"):
        raise ApiProblem(409, "VERIFICATION_REQUIRED", "Wait for the verification result before publishing.")
