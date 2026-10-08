"""Tahap 3: agent verifikasi. Yang dikunci di sini adalah KONTRAK-nya; wiring ADK belum diisi
karena ADK 2.0 punya breaking changes dari 1.x. Ikuti dokumentasi versi yang kamu install."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AgentVerdict(BaseModel):
    """Output schema-locked. Sengaja TIDAK ada field level/alert: agent tidak bisa mengubah level."""
    status: Literal["confirmed", "uncertain", "negative"]
    summary: str
    evidence: list[str] = Field(default_factory=list)
    scene_age_days: int | None = None         # umur scene Landsat yang dipakai ToPeCAl
    topecal_available: bool = False


# --- Tool deterministik (dipanggil agent, dihitung tanpa LLM) ---------------------------------
def fetch_himawari_crop(cell_id: str, around: datetime) -> str:
    """Return path artefak citra hasil crop (visible + thermal) untuk cell itu."""
    raise NotImplementedError


def find_landsat_scene(cell_id: str, before: datetime) -> dict | None:
    """Return metadata scene usable terbaru (id, acquired_at, cloud_cover) atau None."""
    raise NotImplementedError


def run_topecal(scene_id: str, cell_id: str) -> dict:
    """Band math ToPeCAl pada scene. Butuh citra Landsat, jadi tidak bisa jadi bagian rule engine."""
    raise NotImplementedError


def analyze_cell(cell_id: str, now: datetime) -> AgentVerdict:
    """TODO: bungkus tool di atas dalam loop ADK dengan batas iterasi eksplisit dan exit condition.
    Batas panggilan citra juga dijaga di lapisan tool/DB (cooldown), bukan hanya di prompt."""
    raise NotImplementedError