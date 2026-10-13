"""Verification agent (design document sections 7-9): Gemini + `fetch_firms_data`, YAML output.

pipeline/verifier.py locked the contract and left the ADK wiring open; this is that stage, wired. The agent validates
evidence for one region and never sets the risk level (the rules engine does). Its YAML is parsed into AgentVerdict
and checked against what the tool really returned before anything is stored. Limits live in code and in the DB, not
only in the prompt: a hard daily quota (Firestore transaction), at most MAX_TOOL_CALLS tool calls and
MAX_TOOL_CALLS + 1 model calls per run, a short output and an end-to-end timeout. The ADK agent itself is defined in
asapify_agent/agent.py.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import firms
from .config import Settings
from .regions import PROVINCE_NAMES, Region

if TYPE_CHECKING:
    from google.adk.tools import ToolContext

MAX_TOOL_CALLS = 3          # FIRMS requests per verification
MAX_OUTPUT_TOKENS = 1024    # per model call
TIMEOUT_S = 50              # one verification end to end; fits inside the operator's 60 s request
HOURS_WINDOW = (6, 48)      # FIRMS search window the agent may ask for, hours on each side of the observation
BUFFER_KM = (0.0, 25.0)     # extra distance around the region bounds the agent may ask for
EVIDENCE_LIMIT = 15         # hotspots shown to the agent and kept in the record


class SupportingData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    firms_hotspot_count: int = Field(ge=0)
    highest_brightness_temp: float | None = Field(default=None, gt=0)   # K; null when not available


class OperatorAlertDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=1000)


class AgentVerdict(BaseModel):
    """Output schema-locked. Deliberately NO status/level field: the agent cannot change the region's level."""
    model_config = ConfigDict(extra="forbid")
    validation_status: Literal["CONFIRMED", "REJECTED", "NEEDS_MANUAL_REVIEW"]
    confidence_score: float = Field(ge=0, le=1)   # the agent's confidence, not a calibrated fire probability
    analysis: str = Field(min_length=1, max_length=2000)
    supporting_data: SupportingData
    draft_operator_alert: OperatorAlertDraft


class QuotaExhausted(Exception):
    """The daily agent quota is used up; nothing was sent to the model."""


class AgentNotConfigured(Exception):
    """The FIRMS key or the GCP project is missing."""


class VerificationRepo(Protocol):
    def get_region(self, region_id: str) -> dict | None: ...
    def get_hour(self, region_id: str, observed_at: str) -> dict | None: ...
    def reserve_agent_call(self, day: date, limit: int) -> int | None: ...
    def save_verification(self, region_id: str, record: dict) -> None: ...


AgentRun = Callable[[str, dict, Settings], tuple[str, list[dict], dict]]   # (payload, state, settings) -> output


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- output contract --------------------------------------------------------------------------------------------
_FENCE = re.compile(r"^\s*```(?:ya?ml)?\s*\n(.*?)\n?\s*```\s*$", re.S | re.I)


def parse_verdict(text: str) -> AgentVerdict:
    """YAML only (design section 8). A Markdown fence is unwrapped; anything off the schema fails."""
    fenced = _FENCE.match(text or "")
    data = yaml.safe_load(fenced.group(1) if fenced else (text or ""))
    if not isinstance(data, dict):
        raise ValueError("the agent output is not a YAML mapping")
    return AgentVerdict.model_validate(data)


def check_consistency(verdict: AgentVerdict, results: list[dict]) -> list[str]:
    """Backend checks (design section 7.1, step 7): numbers must come from a tool result; CONFIRMED needs evidence."""
    found = [r for r in results if "error" not in r]
    data, problems = verdict.supporting_data, []
    if not found:
        if verdict.validation_status != "NEEDS_MANUAL_REVIEW" or data.firms_hotspot_count:
            problems.append("decided without any FIRMS result")
        return problems
    if data.firms_hotspot_count not in {r["hotspot_count"] for r in found}:
        problems.append("firms_hotspot_count matches no FIRMS result")
    highs = [r["highest_brightness_temp_k"] for r in found if r["highest_brightness_temp_k"] is not None]
    if data.highest_brightness_temp is not None and not any(abs(data.highest_brightness_temp - h) <= 0.05 for h in highs):
        problems.append("highest_brightness_temp matches no FIRMS result")
    if verdict.validation_status == "CONFIRMED" and not any(r.get("confident_count") for r in found):
        problems.append("CONFIRMED without a nominal- or high-confidence hotspot")
    return problems


# --- Deterministic tool (called by the agent, computed without the LLM) -----------------------------------------
def fetch_firms_data(hours_window: int = 24, buffer_km: float = 10.0,
                     tool_context: ToolContext | None = None) -> dict:
    """Fetch NASA FIRMS VIIRS hotspots around the peat region and observation hour under review.

    Args:
        hours_window: Hours before and after the observation time to search, from 6 to 48.
        buffer_km: Extra distance around the region's bounding box, from 0 to 25 km.

    Returns:
        hotspot_count, confident_count (nominal or high confidence), highest_brightness_temp_k, confidence_counts
        and up to 15 hotspots sorted by fire radiative power, each with its distance to the region centre in km
        and its time offset from the observation in hours.
    """
    state = tool_context.state   # the region comes from the backend: the model cannot choose another area
    region, observed_at, now = state["region"], _parse(state["observed_at"]), _parse(state["now"])
    hours = int(min(max(hours_window, HOURS_WINDOW[0]), HOURS_WINDOW[1]))
    buffer = float(min(max(buffer_km, BUFFER_KM[0]), BUFFER_KM[1]))
    west, south, east, north = region["bounds"]
    pad_lat, pad_lon = buffer / 111.0, buffer / (111.0 * math.cos(math.radians(region["lat"])))
    bbox = tuple(round(v, 3) for v in (west - pad_lon, south - pad_lat, east + pad_lon, north + pad_lat))
    start, end = observed_at - timedelta(hours=hours), min(observed_at + timedelta(hours=hours), now)
    first = max(start.date(), end.date() - timedelta(days=firms.MAX_DAY_RANGE - 1))
    query = {"hours_window": hours, "buffer_km": buffer, "from": _iso(start), "to": _iso(end)}
    settings = Settings()
    sources = [s.strip() for s in settings.firms_source.split(",") if s.strip()]

    def fetch(source: str) -> list[dict]:
        return firms.fetch_firms(settings.firms_map_key, source, bbox, (end.date() - first).days + 1, first.isoformat())

    try:
        with ThreadPoolExecutor(max_workers=max(1, len(sources))) as pool:   # sources in parallel: an operator waits
            rows = [row for batch in pool.map(fetch, sources) for row in batch]
    except Exception as exc:   # never pass the message on: an HTTP error can carry the URL, and the URL has the key
        return {**query, "error": f"FIRMS unavailable ({type(exc).__name__})"}
    hotspots = [h for h in firms.normalize(rows) if start <= h["acq_ts"] <= end]
    return {**query, **firms.summarize(hotspots, (region["lat"], region["lon"]), observed_at, EVIDENCE_LIMIT)}


def limit_tool_calls(tool, args: dict, tool_context: ToolContext) -> dict | None:
    """before_tool_callback: at most MAX_TOOL_CALLS FIRMS requests per verification, whatever the model asks."""
    calls = tool_context.state.get("tool_calls", 0)
    if calls >= MAX_TOOL_CALLS:
        return {"error": f"tool call limit ({MAX_TOOL_CALLS}) reached; answer with the data you already have"}
    tool_context.state["tool_calls"] = calls + 1
    return None


# --- one verification -------------------------------------------------------------------------------------------
def build_payload(region: Region, record: dict, trigger: str, now: datetime) -> str:
    """What the backend sends the agent (design section 7.1, step 1), as YAML."""
    inputs = record.get("inputs") or {}
    payload = {
        "region": {"id": region.id, "province": f"{PROVINCE_NAMES.get(region.province, 'unknown')} ({region.province})",
                   "centre": {"lat": region.lat, "lon": region.lon},
                   "bounds_west_south_east_north": list(region.bounds), "peat_km2": region.peat_km2},
        "trigger": "automatic: the region entered ALERT" if trigger == "auto" else "manual: an operator asked",
        "observation": {
            "observed_at": record["observed_at"], "rules_engine_status": record["status"],
            "reason": record.get("reason"), "utilities": record.get("utilities"),
            **{k: inputs.get(k) for k in ("delta_t_k", "skin_temperature_k", "baseline_k",
                                          "relative_humidity_pct", "cloud_cover_pct")},
            "quality": record.get("quality"), "flags": record.get("flags"),
            "sources": "NASA POWER skin temperature and humidity (reanalysis); Open-Meteo cloud cover (model)",
        },
        "current_time": _iso(now),
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def _evidence(verdict: AgentVerdict | None, found: list[dict]) -> list[dict]:
    """Hotspots of the tool result the verdict relies on (else the last one), for the operator's map."""
    if not found:
        return []
    count = verdict.supporting_data.firms_hotspot_count if verdict else None
    used = next((r for r in reversed(found) if r["hotspot_count"] == count), found[-1])
    return used.get("hotspots", [])


def analyze_region(region: Region, record: dict, now: datetime, *, trigger: str, actor: str | None = None,
                   settings: Settings | None = None, run: AgentRun | None = None) -> dict:
    """One agent run -> the verification record. Agent failures become NEEDS_MANUAL_REVIEW records; nothing raises."""
    settings = settings or Settings()
    state = {"region": {"id": region.id, "lat": region.lat, "lon": region.lon, "bounds": list(region.bounds)},
             "observed_at": record["observed_at"], "now": _iso(now), "tool_calls": 0}
    started = time.monotonic()
    text, results, usage, error = "", [], {}, None
    # 1) the ADK loop, bounded by the tool and model call limits and a timeout
    try:
        text, results, usage = (run or run_agent)(build_payload(region, record, trigger, now), state, settings)
    except TimeoutError:
        error = "AGENT_TIMEOUT"
    except Exception as exc:
        name = type(exc).__name__
        error = "LLM_CALL_LIMIT" if name == "LlmCallsLimitExceededError" else f"AGENT_ERROR:{name}"
    # 2) schema first, then consistency with what the tool really returned
    verdict, checks = None, []
    if error is None:
        try:
            verdict = parse_verdict(text)
        except (yaml.YAMLError, ValidationError, ValueError):
            error = "INVALID_OUTPUT"
    if verdict is not None:
        checks = check_consistency(verdict, results)
    found = [r for r in results if "error" not in r]
    return {
        "id": f"{now:%Y%m%dT%H%M%SZ}", "region_id": region.id, "trigger": trigger, "requested_by": actor,
        "at": _iso(now), "observed_at": record["observed_at"], "region_status": record["status"],
        "validation_status": verdict.validation_status if verdict and not checks else "NEEDS_MANUAL_REVIEW",
        "agent_validation_status": verdict.validation_status if verdict else None,
        "confidence_score": verdict.confidence_score if verdict else None,
        "analysis": verdict.analysis if verdict else f"The agent result could not be used ({error}). "
                                                     "Check the FIRMS evidence manually.",
        "supporting_data": verdict.supporting_data.model_dump() if verdict else None,
        "draft_operator_alert": verdict.draft_operator_alert.model_dump() if verdict else None,
        "checks": checks, "error": error,
        "tool_calls": [{k: r.get(k) for k in ("hours_window", "buffer_km", "from", "to", "hotspot_count",
                                              "confident_count", "highest_brightness_temp_k", "error")}
                       for r in results],
        "evidence": _evidence(verdict, found),
        "model": settings.gemini_model, "usage": usage, "duration_s": round(time.monotonic() - started, 1),
        "raw_output": (text or "")[:4000], "decision": None,
    }


def verify_region(repo: VerificationRepo, region: Region, now: datetime, *, trigger: str,
                  observed_at: str | None = None, actor: str | None = None, settings: Settings | None = None,
                  run: AgentRun | None = None) -> dict:
    """Quota first (DB layer, not the prompt), then one agent run; the record is stored and returned."""
    settings = settings or Settings()
    if not settings.agent_ready:
        raise AgentNotConfigured()
    doc = repo.get_region(region.id) or {}
    record = (repo.get_hour(region.id, observed_at) if observed_at else None) or doc.get("latest")
    if not record:
        raise LookupError(f"{region.id} has no assessed hour yet")
    if repo.reserve_agent_call(now.date(), settings.agent_daily_quota) is None:
        raise QuotaExhausted()
    result = analyze_region(region, record, now, trigger=trigger, actor=actor, settings=settings, run=run)
    repo.save_verification(region.id, result)
    return result


# --- ADK runtime ------------------------------------------------------------------------------------------------
def run_agent(payload: str, state: dict, settings: Settings) -> tuple[str, list[dict], dict]:
    """The ADK loop with an explicit iteration limit, as pipeline/verifier.py planned. Called from a worker thread."""
    import google.adk.runners  # noqa: F401  imported before the clock starts: a cold import takes seconds
    import asapify_agent.agent  # noqa: F401
    return asyncio.run(asyncio.wait_for(_run_adk(payload, state, settings), TIMEOUT_S))


async def _run_adk(payload: str, state: dict, settings: Settings) -> tuple[str, list[dict], dict]:
    from google.adk.agents import RunConfig
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    from asapify_agent.agent import build_agent

    runner = InMemoryRunner(agent=build_agent(settings.gemini_model), app_name="asapify")
    text, results = "", []
    usage = {"model_calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thought_tokens": 0}
    try:
        session = await runner.session_service.create_session(app_name="asapify", user_id="engine", state=state)
        message = types.Content(role="user", parts=[types.Part(text=payload)])
        async for event in runner.run_async(user_id="engine", session_id=session.id, new_message=message,
                                            run_config=RunConfig(max_llm_calls=MAX_TOOL_CALLS + 1)):
            results += [dict(r.response or {}) for r in event.get_function_responses()]
            meta = event.usage_metadata
            if meta is not None:
                usage["model_calls"] += 1
                usage["prompt_tokens"] += meta.prompt_token_count or 0
                usage["output_tokens"] += meta.candidates_token_count or 0
                usage["thought_tokens"] += meta.thoughts_token_count or 0
            if event.is_final_response() and event.content and event.content.parts:
                text = "".join(p.text or "" for p in event.content.parts if not p.thought)
    finally:
        await runner.close()
    return text, results, usage
