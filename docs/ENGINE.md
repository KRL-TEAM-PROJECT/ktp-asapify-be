# Risk engine, for readers of `pipeline/`

`asapify_engine/` is the live engine. It implements the BE team's design document (`asapify_rancangan_proyek.md`)
and keeps the conventions of `pipeline/` from the initial commit: frozen dataclasses, a `Status` enum, a `RuleConfig`
of hypotheses, pure rule functions, a `Repo` protocol with a `run_cycle`, and an agent contract in `verifier.py`.
If you wrote `pipeline/`, this page tells you where each piece went and what changed on purpose.

## Where things went

| `pipeline/` (8 Oct) | `asapify_engine/` (now) | Note |
|---|---|---|
| `models.py` `Status` normal / watch / triggered / no_observation | `models.py` `Status` SAFE / WARNING / ALERT / NO_OBSERVABLE | Design order, in English |
| `models.py` `ThermalObs` | `canonical.py` `Observation` | Design section 3.3, with ACCEPTED / DEGRADED / REJECTED quality |
| `models.py` `CellState` | `models.py` `RegionState` and `Transition` | Every status change is kept for audit |
| `rules.py` `RuleConfig` (z_watch, z_trigger, cooldown) | `rules.py` `RuleConfig` (trapezoids, cloud_max, safety_margin, cooldown) | Still hypotheses: calibrate with a backtest |
| `rules.py` `robust_z`, `evaluate_cell` | `canonical.py` `anomaly`, `rules.py` `assess`, `fsm.py` `step` | ΔT against a baseline, then Fuzzy MAUT, then the state machine |
| `rules.py` `should_trigger_agent(prev, new, now, cfg)`, `mark_agent_called` | `fsm.py`, same names and signature | Transition into ALERT plus cooldown, not the condition |
| `pipeline.py` `Repo`, `run_cycle` | `ingest.py` `Repo`, `run_cycle`; `repo.py` `FirestoreRepo` | Postgres became Firestore |
| `himawari.py` `HimawariSource` | `power.py`, `openmeteo.py` | Source adapters that return canonical observations |
| `peat_mask.py` `build_cells`, `peat_fraction` | `tools/build_regions.py` -> `regions.json` | A region is a NASA POWER cell with at least 25 km² of KHG peat |
| `firms.py` `fetch_firms`, `normalize` | `firms.py`, same names, plus `summarize` | stdlib `csv` instead of pandas; errors never carry the key |
| `verifier.py` `AgentVerdict`, tools, `analyze_cell` | `verifier.py` `AgentVerdict`, `fetch_firms_data`, `analyze_region`; `asapify_agent/agent.py` `root_agent` | The ADK loop is wired now |
| `config.py` `Settings` | `config.py` `Settings` | Read when constructed, not at import |

## Breaking changes

1. **Status names and order.** SAFE < WARNING < ALERT, plus NO_OBSERVABLE. WARNING is the middle level now; the
   top level is ALERT. Compact codes stay `S`, `W`, `A`, `N`, so `A` means ALERT. The legacy pixel/cluster contract
   follows: `warning_clusters` is `alert_clusters` and `warning_nights` is `alert_nights`.
2. **Unit of observation.** A Himawari pixel (`h{row}_{col}`) became a peat region, one POWER grid cell of
   0.5° x 0.625°, with an id like `R2.50S113.750E` (centre latitude, then longitude).
3. **Thermal input.** ΔBT (B07 − B13) scored with a robust z became ΔT: NASA POWER skin temperature minus the mean
   skin temperature of the same UTC hour, ±15 calendar days, 2021-2025. Design section 3.4 requires the observation
   and the baseline to be the same physical measurement; POWER provides both for every year. The night streak and
   neighbour support are not used: POWER is hourly and has no night slots.
4. **Inputs per hour.** Skin temperature and relative humidity from NASA POWER (reanalysis, about 2-3 days behind),
   cloud cover from Open-Meteo (model; POWER's cloud amount for 2026 is not published yet). Cloud cover above 60 %
   makes the hour NO_OBSERVABLE, as the design says.
5. **Storage.** Firestore database `asapify`, see the top of `repo.py` for every collection.
6. **Agent output.** `status confirmed / uncertain / negative` became the design's YAML schema with English keys:
   `validation_status` (CONFIRMED / REJECTED / NEEDS_MANUAL_REVIEW), `confidence_score`, `analysis`,
   `supporting_data` and `draft_operator_alert`. The verdict still has no level field: the agent cannot change it.

## One cycle

Cloud Scheduler calls `POST /_internal/engine/tick?batch=20&budget=240` on the private worker every 15 minutes.

1. Agent runs that waited from an earlier cycle go first.
2. One request asks POWER for its latest published hour. If every region already has it, the cycle stops here:
   that is most cycles, and it costs one request and one Firestore read.
3. Otherwise the regions that are behind, least recently updated first, are processed until `batch` or `budget`
   runs out. Per region: baseline table (cached, rebuilt about once a week), recent hours, one assessment and one
   audit record per new hour, the state machine hour by hour.
4. A region that entered ALERT gets `last_agent_at` saved first (so the 6 h cooldown holds even if the agent fails)
   and an agent request in `engine_meta/ingestion.agent_pending`. Requests run while at least 60 s of budget is left;
   the rest wait for the next cycle.

## Verification agent

- `asapify_agent/agent.py` defines `root_agent` (ADK `Agent`, name `geospatial_analyst`) with the instruction from
  design section 8 and one tool, `fetch_firms_data`. The tool reads the region from the session state, so the model
  cannot query another area; it may only widen the time window (6-48 h) and the buffer (0-25 km).
- Triggers: automatic on the transition into ALERT, and the operator's Re-verify button
  (`POST /api/v1/operator/regions/{id}/verify`, at most once per region per 5 minutes).
- Limits in code and in the database, not only in the prompt: a hard daily quota taken in a Firestore transaction
  before any model call (`AGENT_DAILY_QUOTA`, default 20), at most 3 tool calls and 4 model calls per run,
  1,024 output tokens per call, minimal thinking, a 50 s timeout, at most 15 hotspots shown to the model.
- The YAML is parsed into `AgentVerdict`, then checked against what the tool really returned: the hotspot count and
  the highest brightness temperature must come from a tool result, and CONFIRMED needs at least one nominal- or
  high-confidence hotspot. Anything else is stored as NEEDS_MANUAL_REVIEW with the reason in `checks`.
- Measured on 13 Oct with live FIRMS data and `gemini-3.5-flash` on Vertex AI: R3.50S105.625E (South Sumatra)
  came back CONFIRMED (1,938 hotspots, 367 K) in 11 s with 2 model calls, about 3,500 input and 350 output tokens.
- Operators publish or reject each verification with `POST /api/v1/operator/regions/{id}/decision`
  (`verification_id`, plus `caption` or `reason`). Publishing reuses the notification engine: one alert per
  verification, `purpose=emergency`, fan-out to subscribers of the region's province. A REJECTED verification
  can only be rejected.

### Try the agent locally

```bash
gcloud auth application-default login
pip install -r requirements-dev.txt
# asapify_agent/.env (git-ignored), never commit it:
#   GOOGLE_GENAI_USE_VERTEXAI=TRUE
#   GOOGLE_CLOUD_PROJECT=hack2skill-510921
#   GOOGLE_CLOUD_LOCATION=global
#   FIRMS_MAP_KEY=<your own key>
adk web   # from the repository root, then pick asapify_agent
```

In `adk web` the tool needs the session state that the backend normally sets (`region`, `observed_at`, `now`), so
use the backend path for real runs; `adk web` is for reading the instruction and trying prompts.

## Configuration

| Variable | Default | Where |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | none | Both services (already set) |
| `FIRESTORE_DATABASE` | `asapify` | Both services |
| `FIRMS_MAP_KEY` | none | Both services, from Secret Manager `firms-map-key` |
| `FIRMS_SOURCE` | `VIIRS_NOAA20_NRT,VIIRS_NOAA21_NRT` | Optional |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Optional |
| `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_LOCATION` | `TRUE`, `global` | Gemini is not served from asia-southeast2 |
| `AGENT_DAILY_QUOTA` | `20` | Hard limit on agent runs per UTC day |

`gcp/agent-setup.sh` sets these once on both services; the deploy pipeline only swaps the image, so they stay.
The worker needs a 300 s request timeout for `budget=240`.

## Adding Himawari back

Write an adapter that returns canonical `Observation`s with its own `measurement_type` (for example
`brightness_temperature_b07`) and build its baseline from the same sensor. Never subtract a skin-temperature
baseline from a brightness temperature: `canonical.anomaly` raises on incompatible measurements on purpose. A new
input then needs its own membership functions in `RuleConfig` and a backtest before it changes any status.

## Tests

`python -m pytest -q` runs everything offline: fake fetchers for POWER and Open-Meteo, an in-memory `Repo`,
a mock FIRMS transport and a scripted model that drives the real ADK runner.
