"""Every test stays offline: the agent's imagery and weather fetchers answer from memory unless a test injects its own."""
import pytest

from asapify_engine import imagery, openmeteo

NO_IMAGE = {"date": "2026-10-09", "hours_before_observation": 16.0, "pictures": [], "errors": ["offline test"]}
NO_WEATHER = {"source": "open_meteo", "at": None}


@pytest.fixture(autouse=True)
def offline_visual_inputs(monkeypatch):
    monkeypatch.setattr(imagery, "fetch", lambda *a, **kw: NO_IMAGE)
    monkeypatch.setattr(openmeteo, "fetch_weather", lambda *a, **kw: NO_WEATHER)
