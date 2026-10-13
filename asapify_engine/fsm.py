"""Finite state machine per observation region: one current state, one record per status change."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .rules import Assessment, Status

AGENT_COOLDOWN = timedelta(hours=6)


@dataclass(frozen=True)
class RegionState:
    region_id: str
    status: Status
    since: datetime              # when the region entered this status
    observed_at: datetime        # observation time of the latest assessment
    utilities: dict[str, float]
    last_agent_at: datetime | None = None


@dataclass(frozen=True)
class Transition:
    region_id: str
    observed_at: datetime
    previous: Status | None
    current: Status
    utilities: dict[str, float]
    reason: str


def step(previous: RegionState | None, region_id: str, observed_at: datetime,
         assessment: Assessment) -> tuple[RegionState, Transition | None]:
    """Apply one assessment. An older or repeated observation never moves the state backwards."""
    if previous is not None and observed_at <= previous.observed_at:
        return previous, None
    changed = previous is None or previous.status is not assessment.status
    state = RegionState(
        region_id=region_id, status=assessment.status,
        since=observed_at if changed else previous.since, observed_at=observed_at,
        utilities=assessment.utilities, last_agent_at=previous.last_agent_at if previous else None)
    if not changed:
        return state, None
    return state, Transition(region_id, observed_at, previous.status if previous else None,
                             assessment.status, assessment.utilities, assessment.reason)


def should_trigger_agent(previous: RegionState | None, current: RegionState, now: datetime,
                         cooldown: timedelta = AGENT_COOLDOWN) -> bool:
    """Verify automatically only on the transition INTO ALERT, and not again within the cooldown."""
    entering = current.status is Status.ALERT and (previous is None or previous.status is not Status.ALERT)
    if not entering:
        return False
    return current.last_agent_at is None or now - current.last_agent_at >= cooldown


def mark_agent_called(state: RegionState, now: datetime) -> RegionState:
    return replace(state, last_agent_at=now)
