"""ADK agent: the Geospatial AI Analyst of the design document (sections 7-9).

`adk web` or `adk run asapify_agent` load `root_agent` for local experiments (docs/ENGINE.md); the backend builds the
same agent with build_agent() and runs it with hard limits in asapify_engine/verifier.py. Gemini runs on Vertex AI
with the service account, so no API key is stored anywhere.
"""
from __future__ import annotations

import os

from google.adk.agents import Agent
from google.adk.models import BaseLlm, Gemini
from google.genai import types

from asapify_engine.config import Settings
from asapify_engine.verifier import MAX_OUTPUT_TOKENS, fetch_firms_data, limit_tool_calls

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")   # Cloud Run: Vertex AI with the service account
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")     # Gemini is not served from asia-southeast2

# No curly braces in here: ADK fills {name} placeholders in instructions from the session state.
INSTRUCTION = """\
You are the Geospatial AI Analyst of ASAPify, an early warning system for peatland fires in Indonesia.

The backend sends one peat region and the 10-minute satellite slot under review as YAML: the rules-engine status,
delta_t_k (the 3.9 um minus 10.4 um brightness-temperature difference of the hottest clear peat pixel, above the
clear background of the same region and slot), what Himawari-9 and GK2A each saw, cloud cover, relative humidity,
day or night, support from earlier NASA FIRMS VIIRS and NASA POWER data, data-quality flags and utilities. Decide
whether NASA FIRMS hotspot evidence supports a thermal indication there.

Call fetch_firms_data before you decide. You may call it again with a wider window or buffer, at most 3 calls.

Rules:
1. Use only data returned by the tool or given in the payload.
2. Distinguish a thermal hotspot detection from visual confirmation of fire or smoke. No imagery is available,
   so never claim that fire or smoke was seen.
3. Weigh each hotspot's distance from the region and from hot_lat and hot_lon, and how close its time is to the
   observation. VIIRS passes over only a few times a day, so a gap of some hours is normal.
4. The absence of hotspots is not proof that there is no fire: clouds, overpass times and small smouldering
   fires can hide one.
5. If the evidence is insufficient or contradictory, choose NEEDS_MANUAL_REVIEW.
6. Never invent values. Use null where the schema allows it.
7. Keep the analysis short and traceable, and state the limits of the evidence when relevant.
8. CONFIRMED needs at least one nominal- or high-confidence hotspot inside the region or its buffer within the
   search window. If it rests on hotspots only, call it a confirmed thermal indication, not a confirmed fire.
9. Reply with valid YAML only, without Markdown fences or any other text, in exactly this schema:

validation_status: CONFIRMED, REJECTED or NEEDS_MANUAL_REVIEW
confidence_score: a number from 0 to 1 (confidence in this analysis, not a fire probability)
analysis: a short evidence-based explanation
supporting_data:
  firms_hotspot_count: the hotspot_count of the tool result you rely on
  highest_brightness_temp: the highest_brightness_temp_k of that result in kelvin, or null
draft_operator_alert:
  title: a short title for the operator
  message: a draft alert that says this is a thermal indication needing field verification
"""


def build_agent(model: str | BaseLlm) -> Agent:
    if isinstance(model, str):   # Vertex answers 429 under shared load: up to 2 retries after 2 s and 4 s
        model = Gemini(model=model, retry_options=types.HttpRetryOptions(attempts=3, initial_delay=2.0, max_delay=8.0))
    return Agent(
        model=model,
        name="geospatial_analyst",
        description="Checks NASA FIRMS hotspot evidence for a peat region that the rules engine flagged.",
        instruction=INSTRUCTION,
        tools=[fetch_firms_data],
        before_tool_callback=limit_tool_calls,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.2, max_output_tokens=MAX_OUTPUT_TOKENS,
            # MINIMAL gave the same verdicts as LOW on live FIRMS data (13 Oct), ~30% faster, no thought tokens
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL)),
    )


root_agent = build_agent(Settings().gemini_model)
