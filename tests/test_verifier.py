from datetime import date, datetime, timezone
from typing import AsyncGenerator

import httpx
import pytest
import yaml

from asapify_engine import firms, verifier
from asapify_engine.config import Settings
from asapify_engine.regions import Region

UTC = timezone.utc
NOW = datetime(2026, 10, 13, 6, tzinfo=UTC)
REGION = Region("R2.50S113.750E", -2.5, 113.75, (113.4375, -2.75, 114.0625, -2.25), "62", 812.0)
VIEW = {"n_pixels": 420, "n_peat": 160, "n_peat_clear": 150, "cloud_fraction": 0.0625, "background_k": 2.1,
        "background_mad_k": 0.4, "max_anomaly_k": 8.4, "max_z": 21.0, "n_hot": 2, "hot_lat": -2.51, "hot_lon": 113.8,
        "mwir_k": 309.4, "lwir_k": 298.9}
RECORD = {"observed_at": "2026-10-10T06:00:00Z", "status": "ALERT", "reason": "ALERT has the highest utility (0.900)",
          "utilities": {"SAFE": 0.0, "WARNING": 0.1, "ALERT": 0.9}, "night": True, "sun_elevation_deg": -42.0,
          "views": {"himawari9": VIEW, "gk2a": {**VIEW, "max_anomaly_k": 9.1}},
          "inputs": {"delta_t_k": 8.4, "cloud_cover_pct": 6.2, "relative_humidity_pct": 27.0,
                     "hot_lat": -2.51, "hot_lon": 113.8},
          "support": {"viirs_support": 0.8}, "quality": {"thermal": "ACCEPTED", "relative_humidity": "ACCEPTED"},
          "flags": ["two_satellites_agree", "model_estimate"]}
SETTINGS = Settings(project="test", firms_map_key="KEY", firms_source="VIIRS_NOAA20_NRT",
                    gemini_model="gemini-test", agent_daily_quota=2)
YAML_OK = """\
validation_status: CONFIRMED
confidence_score: 0.8
analysis: Two hotspots about 6 km from the centre, one nominal, within an hour of the observation.
supporting_data:
  firms_hotspot_count: 2
  highest_brightness_temp: 330.5
draft_operator_alert:
  title: Thermal indication in Central Kalimantan
  message: FIRMS shows hotspots near the region. This is a thermal indication; verify it in the field.
"""
HOTSPOT = {"lat": -2.51, "lon": 113.8, "acquired_at": "2026-10-10T05:42:00Z", "confidence": "n", "frp_mw": 12.3}
TOOL = {"hours_window": 24, "buffer_km": 10.0, "hotspot_count": 2, "confident_count": 1,
        "highest_brightness_temp_k": 330.5, "hotspots": [HOTSPOT]}


def fake_run(text, results, region=REGION):
    def run(payload, state, settings):
        assert region.id in payload and state["region"]["id"] == region.id and state["tool_calls"] == 0
        return text, results, {"model_calls": 2}
    return run


def analyze(run, record=RECORD):
    return verifier.analyze_region(REGION, record, NOW, trigger="auto", settings=SETTINGS, run=run)


# ---------- output contract ----------

def test_parse_accepts_plain_or_fenced_yaml_and_nothing_else():
    assert verifier.parse_verdict(YAML_OK).validation_status == "CONFIRMED"
    assert verifier.parse_verdict("```yaml\n" + YAML_OK + "```").confidence_score == 0.8
    with pytest.raises(Exception):
        verifier.parse_verdict(YAML_OK + "status: SAFE\n")   # the agent cannot set the risk level
    with pytest.raises(Exception):
        verifier.parse_verdict("validation_status: MAYBE\n")


def test_payload_carries_the_region_and_the_slot_under_review():
    payload = verifier.build_payload(REGION, RECORD, "auto", NOW)
    assert "Central Kalimantan (62)" in payload and "delta_t_k: 8.4" in payload
    assert "rules_engine_status: ALERT" in payload and "entered ALERT" in payload
    data = yaml.safe_load(payload)["observation"]
    assert data["satellites"]["gk2a"] == {"max_anomaly_k": 9.1, "max_z": 21.0, "n_hot": 2, "cloud_fraction": 0.0625,
                                          "hot_lat": -2.51, "hot_lon": 113.8}
    assert data["night"] is True and data["support"] == {"viirs_support": 0.8} and "background_k" not in str(data)


def test_confirmed_verdict_backed_by_the_tool_is_kept():
    record = analyze(fake_run(YAML_OK, [TOOL]))
    assert record["validation_status"] == "CONFIRMED" and record["checks"] == [] and record["error"] is None
    assert record["supporting_data"] == {"firms_hotspot_count": 2, "highest_brightness_temp": 330.5}
    assert record["evidence"] == [HOTSPOT] and record["id"] == "20261013T060000Z"
    assert record["tool_calls"][0]["hotspot_count"] == 2 and record["region_status"] == "ALERT"


def test_numbers_no_tool_returned_downgrade_to_manual_review():
    invented = analyze(fake_run(YAML_OK.replace("firms_hotspot_count: 2", "firms_hotspot_count: 9"), [TOOL]))
    assert invented["validation_status"] == "NEEDS_MANUAL_REVIEW" and invented["agent_validation_status"] == "CONFIRMED"
    assert invented["checks"] == ["firms_hotspot_count matches no FIRMS result"]
    without_tool = analyze(fake_run(YAML_OK, []))
    assert without_tool["validation_status"] == "NEEDS_MANUAL_REVIEW"


def test_confirmed_needs_a_nominal_or_high_confidence_hotspot():
    record = analyze(fake_run(YAML_OK, [{**TOOL, "confident_count": 0}]))
    assert record["validation_status"] == "NEEDS_MANUAL_REVIEW"
    assert "CONFIRMED without a nominal- or high-confidence hotspot" in record["checks"]


@pytest.mark.parametrize("failure, code", [(TimeoutError(), "AGENT_TIMEOUT"),
                                           (RuntimeError("boom"), "AGENT_ERROR:RuntimeError")])
def test_agent_failures_become_manual_review_records(failure, code):
    def run(payload, state, settings):
        raise failure
    record = analyze(run)
    assert record["validation_status"] == "NEEDS_MANUAL_REVIEW" and record["error"] == code
    assert record["draft_operator_alert"] is None and "boom" not in record["analysis"]


def test_free_text_is_recorded_but_not_trusted():
    record = analyze(fake_run("I think there is a fire.", [TOOL]))
    assert record["error"] == "INVALID_OUTPUT" and record["validation_status"] == "NEEDS_MANUAL_REVIEW"
    assert record["raw_output"] == "I think there is a fire." and record["evidence"] == [HOTSPOT]


# ---------- tool and limits ----------

class Ctx:
    """The part of ADK's ToolContext that the tool and the callback use."""
    def __init__(self, state):
        self.state = state


def state():
    return {"region": {"id": REGION.id, "lat": REGION.lat, "lon": REGION.lon, "bounds": list(REGION.bounds)},
            "observed_at": "2026-10-10T06:00:00Z", "now": "2026-10-13T06:00:00Z", "tool_calls": 0}


def row(day, hhmm, confidence="n"):
    return {"latitude": "-2.51", "longitude": "113.80", "bright_ti4": "330.5", "acq_date": day, "acq_time": hhmm,
            "satellite": "N20", "confidence": confidence, "frp": "12.3", "daynight": "D"}


def test_tool_clamps_the_request_and_queries_only_the_region_under_review(monkeypatch):
    calls = []

    def fetch(key, source, bbox, day_range, date):
        calls.append((key, source, bbox, day_range, date))
        return [row("2026-10-10", "0542"), row("2026-10-12", "0700")]   # the second is after the window

    monkeypatch.setattr(firms, "fetch_firms", fetch)
    monkeypatch.setenv("FIRMS_MAP_KEY", "KEY")
    monkeypatch.setenv("FIRMS_SOURCE", "VIIRS_NOAA20_NRT")
    result = verifier.fetch_firms_data(hours_window=999, buffer_km=-5, tool_context=Ctx(state()))
    assert result["hours_window"] == 48 and result["buffer_km"] == 0.0
    key, source, bbox, day_range, first = calls[0]
    assert (key, source, day_range, first) == ("KEY", "VIIRS_NOAA20_NRT", 5, "2026-10-08")
    assert all(abs(a - b) < 0.001 for a, b in zip(bbox, REGION.bounds))
    assert result["hotspot_count"] == 1 and result["to"] == "2026-10-12T06:00:00Z"


def test_tool_hides_upstream_errors(monkeypatch):
    def fetch(*args):
        raise httpx.ConnectError("https://firms.example/api/area/csv/SECRET-KEY/...")
    monkeypatch.setattr(firms, "fetch_firms", fetch)
    result = verifier.fetch_firms_data(tool_context=Ctx(state()))
    assert result["error"] == "FIRMS unavailable (ConnectError)" and "SECRET-KEY" not in str(result)


def test_callback_caps_tool_calls():
    ctx = Ctx({"tool_calls": 0})
    assert [verifier.limit_tool_calls(None, {}, ctx) for _ in range(3)] == [None, None, None]
    assert "limit" in verifier.limit_tool_calls(None, {}, ctx)["error"] and ctx.state["tool_calls"] == 3


class MemoryRepo:
    def __init__(self, used=0):
        self.used, self.saved = used, []

    def get_region(self, region_id):
        return {"id": region_id, "latest": RECORD}

    def get_slot(self, region_id, observed_at):
        return None

    def reserve_agent_call(self, day: date, limit):
        if self.used >= limit:
            return None
        self.used += 1
        return self.used

    def save_verification(self, region_id, record):
        self.saved.append(record)


def test_quota_is_taken_before_the_model_and_records_are_saved():
    repo = MemoryRepo()
    record = verifier.verify_region(repo, REGION, NOW, trigger="manual", actor="op@example.test",
                                    settings=SETTINGS, run=fake_run(YAML_OK, [TOOL]))
    assert repo.saved == [record] and record["requested_by"] == "op@example.test" and repo.used == 1
    full = MemoryRepo(used=2)
    with pytest.raises(verifier.QuotaExhausted):
        verifier.verify_region(full, REGION, NOW, trigger="manual", settings=SETTINGS,
                               run=lambda *a: pytest.fail("the model must not be called"))
    with pytest.raises(verifier.AgentNotConfigured):
        verifier.verify_region(MemoryRepo(), REGION, NOW, trigger="auto", settings=Settings(project="", firms_map_key=""))


# ---------- ADK wiring, offline ----------

def test_adk_agent_exposes_only_the_two_tool_parameters():
    from google.adk.tools import FunctionTool

    from asapify_agent.agent import INSTRUCTION, build_agent
    agent = build_agent("gemini-3.5-flash")
    assert agent.generate_content_config.max_output_tokens == verifier.MAX_OUTPUT_TOKENS
    assert agent.model.model == "gemini-3.5-flash" and agent.model.retry_options.attempts == 3
    assert "{" not in INSTRUCTION and "}" not in INSTRUCTION   # ADK would treat braces as state placeholders
    declaration = FunctionTool(verifier.fetch_firms_data)._get_declaration()
    schema = declaration.parameters_json_schema   # tool_context is injected by ADK, never shown to the model
    assert schema["properties"].keys() == {"hours_window", "buffer_km"}
    assert schema["properties"]["hours_window"]["type"] == "integer"


def test_adk_loop_runs_the_tool_and_returns_the_final_yaml(monkeypatch):
    """The real ADK runner with a scripted model: one tool call, then the YAML answer. No network."""
    from google.adk.models import BaseLlm, LlmResponse
    from google.genai import types

    import asapify_agent.agent as agent_module

    class ScriptedLlm(BaseLlm):
        model: str = "scripted"

        async def generate_content_async(self, llm_request, stream=False) -> AsyncGenerator[LlmResponse, None]:
            asked = any(p.function_response for c in llm_request.contents for p in (c.parts or []))
            part = (types.Part(text=YAML_OK.replace("firms_hotspot_count: 2", "firms_hotspot_count: 1")) if asked
                    else types.Part(function_call=types.FunctionCall(name="fetch_firms_data", args={"hours_window": 24})))
            yield LlmResponse(content=types.Content(role="model", parts=[part]),
                              usage_metadata=types.GenerateContentResponseUsageMetadata(
                                  prompt_token_count=100, candidates_token_count=20))

    real_build = agent_module.build_agent
    monkeypatch.setattr(agent_module, "build_agent", lambda model: real_build(ScriptedLlm()))
    monkeypatch.setattr(firms, "fetch_firms", lambda *a: [row("2026-10-10", "0542")])
    record = analyze(None)   # the default runner: verifier.run_agent
    assert record["error"] is None, record["raw_output"]
    assert record["validation_status"] == "CONFIRMED" and record["tool_calls"][0]["hotspot_count"] == 1
    assert record["usage"]["model_calls"] == 2 and record["usage"]["prompt_tokens"] == 200
