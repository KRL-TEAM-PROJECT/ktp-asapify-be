"""Stage 3: verification agent. What is locked here is the CONTRACT; the ADK wiring is not done yet
because ADK 2.0 has breaking changes from 1.x. Follow the docs for the version you install."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AgentVerdict(BaseModel):
    """Schema-locked output. Deliberately NO level/alert field: the agent cannot change the level."""
    status: Literal["confirmed", "uncertain", "negative"]
    summary: str
    evidence: list[str] = Field(default_factory=list)
    scene_age_days: int | None = None         # age of the Landsat scene used by ToPeCAl
    topecal_available: bool = False


# --- Deterministic tools (called by the agent, computed without an LLM) ---------------------
def fetch_himawari_crop(cell_id: str, around: datetime) -> str:
    """Return the paths of the cropped image artefacts (visible + thermal) for that cell."""
    raise NotImplementedError


def find_landsat_scene(cell_id: str, before: datetime) -> dict | None:
    """Return metadata of the newest usable scene (id, acquired_at, cloud_cover) or None."""
    raise NotImplementedError


def run_topecal(scene_id: str, cell_id: str) -> dict:
    """ToPeCAl band math on a scene. Needs Landsat imagery, so it cannot be part of the rule engine."""
    raise NotImplementedError


def analyze_cell(cell_id: str, now: datetime) -> AgentVerdict:
    """TODO: wrap the tools above in an ADK loop with an explicit iteration limit and exit condition.
    The imagery call limit is also enforced in the tool/DB layer (cooldown), not only in the prompt."""
    raise NotImplementedError