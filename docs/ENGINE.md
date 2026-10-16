# Risk engine, for readers of `pipeline/`

`pipeline/` was removed on 15 Oct 2026; read it in git history (`git show b6fa6fd:pipeline/rules.py`). Its KHG
shapefile moved to `tools/khg/`.

`asapify_engine/` is the live engine. It implements the BE team's design document (`asapify_rancangan_proyek.md`)
and keeps the conventions of `pipeline/` from the initial commit: frozen dataclasses, a `Status` enum, a `RuleConfig`
of hypotheses, pure rule functions, a `Repo` protocol with a `run_cycle`, and an agent contract in `verifier.py`.
If you wrote `pipeline/`, this page tells you where each piece went and what changed on purpose.

## Where things went

| `pipeline/` (8 Oct) | `asapify_engine/` (now) | Note |
|---|---|---|
| `models.py` `Status` normal / watch / triggered / no_observation | `models.py` `Status` SAFE / WARNING / ALERT / NO_OBSERVABLE | Design order, in English |
| `models.py` `ThermalObs`, `contextualFire.py` `BandObs` | `contextual.py` `RegionThermal` | One per satellite, region and 10-minute slot |
| `models.py` `CellState` | `models.py` `RegionState` and `Transition` | Every status change is kept for audit |
| `rules.py` `RuleConfig` (z_watch, z_trigger, cooldown) | `rules.py` `RuleConfig` (trapezoids, cloud_max, safety_margin, cooldown, support weights) | Still hypotheses: calibrate against VIIRS |
| `contextualFire.py` `delta_bt`, `robust_z` | `contextual.py` `region_thermal` | Same ΔBT and median/MAD, against the same slot's clear pixels of the cell |
| `rules.py` `should_trigger_agent(prev, new, now, cfg)`, `mark_agent_called` | `fsm.py`, same names and signature | Transition into ALERT plus cooldown, not the condition |
| `pipeline.py` `Repo`, `run_cycle` | `geo.py` `run_geo_cycle`, `ingest.py` `Repo`, `run_cycle`; `repo.py` `FirestoreRepo` | Postgres became Firestore |
| `himawari.py` `HimawariSource` (pycontrails, B07 − B13) | `satellites.py` (satpy): Himawari-9 B07 − B13 and GK2A SW038 − IR105 | Two satellites that confirm each other (`fusion.py`) |
| `himawari.py` `_solar_elevation_deg` | `fusion.py` `solar_elevation_deg` | Same NOAA formula; night = sun below −6° |
| `peat_mask.py` `build_cells`, `peat_fraction` | `tools/build_regions.py` -> `regions.json`, `regions_peat.geojson` | A region is a NASA POWER cell with at least 25 km² of KHG peat |
| `firms.py` `fetch_firms`, `normalize` | `firms.py`, same names, plus `summarize`, `area_hotspots` | stdlib `csv` instead of pandas; errors never carry the key |
| `verifier.py` `AgentVerdict`, tools, `analyze_cell` | `verifier.py` `AgentVerdict`, `fetch_firms_data`, `analyze_region`; `asapify_agent/agent.py` `root_agent` | The ADK loop is wired now |
| `config.py` `Settings` | `config.py` `Settings` | Read when constructed, not at import |

## Breaking changes

1. **Status names and order.** SAFE < WARNING < ALERT, plus NO_OBSERVABLE. WARNING is the middle level now; the
   top level is ALERT. Compact codes stay `S`, `W`, `A`, `N`, so `A` means ALERT.
2. **Unit of observation.** A Himawari pixel (`h{row}_{col}`) became a peat region, one POWER grid cell of
   0.5° x 0.625°, with an id like `R2.50S113.750E` (centre latitude, then longitude). Every satellite pixel whose
   centre lies in the cell is scored; the ones on KHG peat are marked (`n_hot_peat`, `hot_on_peat`).
3. **Thermal input.** ΔBT = BT(3.8-3.9 µm) − BT(10.4-10.5 µm), as in `contextualFire.py`, per satellite and 10-minute
   slot. The baseline is the median ΔBT of the clear pixels of the same cell, slot and sensor (design section 3.4:
   the same measurement), with a robust z from the MAD. A pixel is fire-like when its ΔBT is ≥ 3 z above that
   background **and** the 3.9 µm channel itself is ≥ 2 K and ≥ 3 z warmer than the background; it is hot when its
   ΔBT anomaly is also ≥ 3 K. The 3.9 µm test is the MODIS/VIIRS T4 test: thin cirrus raises ΔBT by cooling 10.4 µm,
   a fire warms 3.9 µm. `delta_t_k` for the rules is the anomaly of the strongest fire-like pixel (0 when there is
   none), so a noisy background cannot reach WARNING on its own. Night streaks and a per-pixel history are not used
   yet; they can join once enough slots are stored.
4. **Two satellites confirm each other** (`fusion.py`). Himawari-9 (140.7° E) and GK2A (128.2° E) see the same peat
   from different angles. Both hot within 8 km = confirmed; both quiet = confirmed quiet. In both cases ΔT is the
   smaller of the two, the size both satellites see. Otherwise they disagree: ΔT is the larger one, and the view is
   DEGRADED (below).
5. **Quality, design section 3.5.** ACCEPTED only at night (sun below −6°) with both satellites agreeing. Daytime,
   one satellite or disagreement is DEGRADED and is **capped at WARNING**, so ALERT always rests on a night view both
   satellites confirm. Less than 40 % of the cell clear in both views is REJECTED: NO_OBSERVABLE, never SAFE.
6. **Supporting data.** NASA FIRMS VIIRS (within 10 km and 12 h of the hot pixel) and the latest NASA POWER
   skin-temperature anomaly (at most 4 days old) only count at a possible hotspot, and only **raise** the ALERT
   utility (`rules.assess` keeps the larger of the utility with and without them): no hotspot in other data is not
   proof of no fire. POWER no longer sets any status; its hourly cycle stays and writes the region's `power` summary.
7. **Other inputs.** Cloud comes from the satellites' own 10.4 µm channel (colder than 270 K = cloud top). Relative
   humidity is Open-Meteo's model value for the slot's hour (one request for all regions per hour); without it the
   rules use the thermal input alone and say so in the reason. A missing, non-finite or out-of-range input counts
   as unavailable, never as a value: no ΔT or cloud is NO_OBSERVABLE, humidity outside 0-100 % is left out, and
   supporting values are kept within 0-1.
8. **ALERT needs heat.** Humidity and supporting data only weigh in when ΔT has a high membership: with
   `delta_t_high` = 0 the ALERT utility is set to 0 and the reason says so. Before this, dry air (RH < 30 %) alone
   gave ALERT 0.4 and, with ΔT around 1.5-1.9 K, won through the safety margin.
9. **Storage.** Firestore database `asapify`, see the top of `repo.py` for every collection. Slot records live in
   `engine_regions/{id}/slots/{YYYYMMDDHHMM}`; POWER hours (support only) stay in `hours`; the map's day archive
   lives in `engine_days/{YYYY-MM-DD}` (below).
10. **Agent output.** `status confirmed / uncertain / negative` became the design's YAML schema with English keys:
   `validation_status` (CONFIRMED / REJECTED / NEEDS_MANUAL_REVIEW), `confidence_score`, `analysis`,
   `supporting_data` and `draft_operator_alert`. The verdict still has no level field: the agent cannot change it.

## Two cycles on the private worker

**Geostationary, every 10 minutes:** `POST /_internal/engine/geo-tick?budget=240` (Cloud Scheduler job
`asapify-geo-tick`, minutes 5, 15, ... 55).

1. Agent runs that waited from an earlier cycle go first.
2. The slot: the newest one both satellites published (Himawari lands about 12 minutes after the scan, GK2A about
   22-24 minutes, measured on 13 Oct), or a slot older than 40 minutes that only one of them published. HEAD
   requests only; a processed slot is never read again (`engine_meta/ingestion.geo_slot`).
3. Each satellite: download to a temporary folder (Himawari 4 files, ~11 MB: segments 5-6 of B07 and B13; GK2A
   2 full-disk files, ~60 MB), decode with satpy, score all 65 regions. A broken file only drops that satellite.
4. Per region: fusion, then the rules with the cap, then the state machine. Humidity is cached per hour in meta;
   FIRMS is asked once, and only when some region has a hot pixel. One Firestore batch writes every state, slot
   record and transition, and the slot's entry in the day archive.
5. A region that entered ALERT gets `last_agent_at` saved first (so the 6 h cooldown holds even if the agent fails)
   and an agent request in `engine_meta/ingestion.agent_pending`. Requests run while at least 60 s of budget is left;
   the rest wait for the next cycle.

**NASA POWER support, every 15 minutes:** `POST /_internal/engine/tick?batch=20&budget=240` (job
`asapify-engine-tick`). One request asks POWER for its latest published hour; most cycles stop there (one Firestore
read). New hours are written per region to `hours` and summarised as `power` on the region document.

## Hot pixels on the map and the day archive

- **Hot pixels.** `contextual.py` keeps up to 5 hot pixels per region and satellite (latitude, longitude, ΔBT above
  the background; strongest first) in the slot record, `views.<satellite>.hot_pixels`. Fusion adds `viirs`: the VIIRS
  detections within the support's limits (10 km, 12 h) of any of those pixels, nearest first, at most 5. Both are
  evidence for the map; the VIIRS support value still comes from the strongest pixel.
- **Day archive.** Each slot also lands in `engine_days/{YYYY-MM-DD}`, one document per WIB calendar day, in the same
  batch: every region's status code (`S`, `W`, `A`, `N`; `-` = not assessed) and ΔT in `regions` order, plus the
  pixels (`[lat, lon, ΔBT]`) and VIIRS (`[lat, lon, minutes from the slot]`) of the regions with a hot pixel, as one
  compact JSON string per slot under its WIB time `HHMM`.
  A slot is kept under 7,000 characters (pixels and VIIRS are cut first, then the weakest regions; `trimmed` says
  so), so a day stays under Firestore's 1 MiB. `alert` / `warning` mark the day for the calendar.
- **Reads.** `GET /api/v1/operator/days?month=YYYY-MM` lists a month's days and the first and last day;
  `GET /api/v1/operator/days/{date}` returns one day in one read; `GET /api/v1/operator/regions/{id}?date=YYYY-MM-DD`
  returns that region's slots of the day (slot records are kept 30 days).

## Validation: 11 October 2026, 18:10 UTC

Night slot, both satellites decoded by this code and compared with NASA FIRMS VIIRS (NOAA-20, NOAA-21, S-NPP;
nominal/high confidence within 90 minutes; 12,463 detections over Sumatra and Kalimantan):

| | Himawari-9 | GK2A |
|---|---|---|
| Regions with a verdict (enough clear pixels) | 49 / 65 | 52 / 65 |
| Hot regions with a VIIRS fire within 1.4 km of the hot pixel | 10 | 10 |
| Hot regions without VIIRS | 1 (R0.50S101.875E) | 0 |
| Regions with VIIRS fires but no hot pixel (small fires, cloud) | 8 | 8 |

Both satellites were hot within 0.4-5.9 km of each other in the same 10 regions, all with VIIRS fires. The one
region only Himawari saw (55 km from GK2A's warmest pixel, no VIIRS) comes out DEGRADED, capped at WARNING. The
strongest anomaly was R3.50S105.625E (South Sumatra): 39 K (Himawari) and 42 K (GK2A), 604 VIIRS detections in the
cell. Two choices come from this slot. The first version (ΔBT only, peat pixels only) had 6 of its 11 hot Himawari
regions without any VIIRS fire nearby (thin cirrus); the 3.9 µm test removed all 6. Scoring the whole cell instead
of the peat pixels alone then found 10 regions with a hot pixel next to a VIIRS fire instead of 4, because most fire pixels sat
just outside the simplified KHG polygons. One slot is not a calibration: repeat with more nights and daytime slots
before trusting any number.

## Cost and limits (billing guardrails)

- **Cloud Run worker** (1 vCPU, 512 MiB, timeout 300 s): measured on one Linux vCPU with this slot, both satellites
  decode in about 18-21 s CPU with a peak of about 200 MB (dask chunks of 4 MiB; with dask's 128 MiB default it was
  ~50 s and 740 MB). With the downloads a tick takes roughly 25-35 s: 144 ticks a day is about 110-150k vCPU-seconds
  and 55-75k GiB-seconds a month, inside Cloud Run's monthly free tier (180k vCPU-s, 360k GiB-s) if the rest stays
  small. No memory change is needed.
- **Downloads** come from NOAA's public buckets on AWS (no key, no egress fee); ingress to Cloud Run is free.
- **Firestore** (`asapify` holds the project's free tier): about 65 slot records + 65 state updates per slot,
  ~19k writes a day, plus ~1.6k for POWER. That is around the free 20k writes a day, so expect cents a month.
  Slot records carry `expire_at` for a TTL policy that drops them after 30 days (TTL deletes are billed, also cents).
  The region page reads 6 slot records per hour shown (24 h by default, at most 72 h = 432 reads; one day by date =
  at most 144). The day archive adds one write per slot (144 a day); the map reads one document per day it opens and
  at most 33 per calendar month shown. A day is at most 1 MiB and is kept without TTL: under 365 MiB a year in the
  worst case, against Firestore's free 1 GiB.
- **Open-Meteo**: one request with 65 locations per hour, about 1,560 of the 10,000 free calls a day.
- **FIRMS**: one request per source per slot, only when a region has a hot pixel.
- **Gemini**: unchanged, `AGENT_DAILY_QUOTA` (20 a day) taken in a transaction before any model call.

## Verification agent

- `asapify_agent/agent.py` defines `root_agent` (ADK `Agent`, name `geospatial_analyst`) with the instruction from
  design section 8 and one tool, `fetch_firms_data`. The tool reads the region from the session state, so the model
  cannot query another area; it may only widen the time window (6-48 h) and the buffer (0-25 km).
- The payload is the slot record: status, utilities, ΔT, both satellites' views, cloud, humidity, day or night, the
  VIIRS/POWER support and the flags.
- Triggers: automatic on the transition into ALERT, and the operator's Re-verify button
  (`POST /api/v1/operator/regions/{id}/verify`, at most once per region per 5 minutes).
- Limits in code and in the database, not only in the prompt: a hard daily quota taken in a Firestore transaction
  before any model call (`AGENT_DAILY_QUOTA`, default 20), at most 3 tool calls and 4 model calls per run,
  1,024 output tokens per call, minimal thinking, a 50 s timeout, at most 15 hotspots shown to the model.
- The YAML is parsed into `AgentVerdict`, then checked against what the tool really returned: the hotspot count and
  the highest brightness temperature must come from a tool result, and CONFIRMED needs at least one nominal- or
  high-confidence hotspot. Anything else is stored as NEEDS_MANUAL_REVIEW with the reason in `checks`.
- Operators publish or reject each verification with `POST /api/v1/operator/regions/{id}/decision`
  (`verification_id`, plus `caption` or `reason`). Publishing reuses the notification engine: one alert per
  verification, `purpose=emergency`, one message per linked Telegram group. A REJECTED verification
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
| `FIRMS_MAP_KEY` | none | Both services, from Secret Manager `firms-map-key`; without it there is no VIIRS support |
| `FIRMS_SOURCE` | `VIIRS_NOAA20_NRT,VIIRS_NOAA21_NRT` | Optional |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Optional |
| `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_LOCATION` | `TRUE`, `global` | Gemini is not served from asia-southeast2 |
| `AGENT_DAILY_QUOTA` | `20` | Hard limit on agent runs per UTC day |

`gcp/agent-setup.sh` sets these once on both services; the deploy pipeline only swaps the image, so they stay.
`gcp/geo-setup.sh` creates the `asapify-geo-tick` job and the TTL policy on `slots.expire_at`. The worker needs a
300 s request timeout for `budget=240`.

## Tests

`python -m pytest -q` runs everything offline: synthetic brightness-temperature grids for the contextual test,
fake satellites, FIRMS and Open-Meteo for the cycle, fake POWER fetchers, an in-memory `Repo`, a mock FIRMS
transport and a scripted model that drives the real ADK runner. Decoding real files is checked by hand against VIIRS
(see Validation); the satellite libraries are only imported when a slot is read.
