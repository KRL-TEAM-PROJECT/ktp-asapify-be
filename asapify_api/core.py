from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

UTC = timezone.utc


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

