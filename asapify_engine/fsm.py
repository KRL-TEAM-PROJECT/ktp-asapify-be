"""Finite state machine per observation region: one current state, one record per status change.

Pure functions, no I/O. The agent trigger works like pipeline/rules.py: it fires on the TRANSITION into ALERT plus a
cooldown, not on the condition, so a region that stays in ALERT does not call the agent again and again.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .models import RegionState, Status, Transition
from .rules import Assessment, RuleConfig


def step(prev: RegionState | None, region_id: str, observed_at: datetime,
         assessment: Assessment) -> tuple[RegionState, Transition | None]:
    """Apply one assessment. An older or repeated observation never moves the state backwards."""
    if prev is not None and observed_at <= prev.observed_at:
        return prev, None
    changed = prev is None or prev.status is not assessment.status
    new = RegionState(region_id, assessment.status, observed_at if changed else prev.since, observed_at,
                      assessment.utilities, prev.last_agent_at if prev else None)
    if not changed:
        return new, None
    return new, Transition(region_id, observed_at, prev.status if prev else None, assessment.status,
                           assessment.utilities, assessment.reason)


def should_trigger_agent(prev: RegionState | None, new: RegionState, now: datetime,
                         cfg: RuleConfig = RuleConfig()) -> bool:
    """Trigger on the TRANSITION into ALERT plus the cooldown, not on the condition. Prevents repeated agent calls."""
    if new.status is not Status.ALERT:
        return False
    if prev is not None and prev.status is Status.ALERT:
        return False
    if new.last_agent_at is not None and now - new.last_agent_at < cfg.cooldown:
        return False
    return True


def mark_agent_called(state: RegionState, now: datetime) -> RegionState:
    return replace(state, last_agent_at=now)
