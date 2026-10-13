"""One cron cycle: read peat cells -> thermal observation -> rule engine -> (if needed) agent."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Protocol

from verifier import AgentVerdict, analyze_cell
from himawari import ThermalSource
from models import CellState, ThermalObs
from rules import RuleConfig, evaluate_cell, mark_agent_called, should_trigger_agent


class Repo(Protocol):
    """Your own Postgres implementation (schema in db/schema.sql)."""
    def list_peat_cells(self) -> list[str]: ...
    def save_obs(self, obs: list[ThermalObs]) -> None: ...
    def get_recent_obs(self, cell_id: str, since: datetime) -> list[ThermalObs]: ...
    def get_baseline(self, cell_id: str, ts: datetime) -> list[float]: ...   # exclude baseline_frozen
    def get_state(self, cell_id: str) -> CellState | None: ...
    def save_state(self, state: CellState) -> None: ...
    def save_analysis(self, cell_id: str, verdict: AgentVerdict) -> None: ...  # operator_status='pending'


def run_cycle(repo: Repo, source: ThermalSource, now: datetime | None = None, cfg: RuleConfig = RuleConfig()) -> None:
    now = now or datetime.now(timezone.utc)
    cells = repo.list_peat_cells()
    repo.save_obs(source.sample_cells(now, cells))

    for cell_id in cells:
        recent = repo.get_recent_obs(cell_id, since=now - timedelta(days=cfg.window_days))
        prev = repo.get_state(cell_id)
        new = evaluate_cell(cell_id, recent, partial(repo.get_baseline, cell_id), prev, now, cfg)

        if should_trigger_agent(prev, new, now, cfg):
            new = mark_agent_called(new, now)      # save first so the cooldown holds even if the agent fails
            repo.save_state(new)
            repo.save_analysis(cell_id, analyze_cell(cell_id, now))
        else:
            repo.save_state(new)