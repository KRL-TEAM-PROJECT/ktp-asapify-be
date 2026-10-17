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
| `models.py` `ThermalObs`, `contextualFire.py` `BandObs` | `contextual.py` `RegionThermal` | One per satellite, region and 10-minute slot (one in about three is processed) |
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
   (left undone at the freeze, 16 October 2026).
4. **Two satellites overlap on purpose** (`fusion.py`). Himawari-9 (140.7° E) and GK2A (128.2° E) see the same peat
   from different angles, so when one is under cloud the other still sees. ΔT is the mean of the satellites that see
   the region, each weighted by the clear share of its view: both count when both see, neither decides alone, and
   one is the backup when the other cannot see. Whether they agree (both hot within 8 km, or both quiet) is recorded
   as quality and flags, not used as a cap. Since 17 October 2026, when both see, the stronger view goes beside the
   mean as `delta_t_max_k` with its validity `max_validity` (that view's clear share, 0 when it has no hot pixel), so
   a hotspot one satellite sees strongly is not averaged away (item 8).
5. **Quality.** ACCEPTED when both satellites agree, DEGRADED with one satellite or when they disagree; `daytime` is a
   flag. Since 16 October 2026 no view caps the status: day, night, one satellite or two all go through the same
   curves (below). Less than 40 % of the cell clear in both views is REJECTED: NO_OBSERVABLE, never SAFE.
6. **Supporting data.** NASA FIRMS VIIRS (within 10 km and 12 h of the hot pixel) and the latest NASA POWER
   skin-temperature anomaly (at most 4 days old, in σ of its normal: no support at 1σ, full at 3σ) only count at a
   possible hotspot, and only **raise** ALERT, and only when ΔT_eff already has a high membership: no hotspot in
   other data is not proof of no fire. POWER sets no status; it gives the normal and the support (below).
7. **Other inputs.** Cloud comes from the satellites' own 10.4 µm channel (colder than 270 K = cloud top). A missing,
   non-finite or out-of-range input counts as unavailable, never as a value: no ΔT or cloud is NO_OBSERVABLE,
   humidity outside 0-100 % is left out, and supporting values are kept within 0-1.
8. **Rules: the environment moves the curves** (`rules.py`, since 16 October 2026). The satellites' ΔT decides; the
   environment does not add weight of its own:

   ```
   ΔT_eff = ΔT + d_rh − d_sun
   d_rh   = 1 K · tanh(dry_z / 2)          dry_z: how much drier than its normal the air is, in σ
   d_sun  = 1 K · max(0, sin(sun elevation))
   SAFE = low(ΔT_eff), WARNING = medium(ΔT_eff), ALERT = high(ΔT_eff)
   ```

   Two thermal attributes since 17 October 2026 (Fuzzy MAUT). The mean is the satellites' consensus, the max the
   strongest valid view; both go through the same shifted curves and are combined as a weighted mean:

   ```
   U_s = (0.6 · μ_s(ΔT_mean_eff) + 0.4 · v · μ_s(ΔT_max_eff)) / (0.6 + 0.4 · v)      v = max_validity, 0-1
   ```

   With one satellite, v = 0 or max = mean, U_s is the membership of the mean, as before. Otherwise the utilities are
   graded: mean 6.5 K and max 9 K (v = 1) give WARNING 0.45, ALERT 0.55 (ALERT, where the mean alone gives WARNING);
   mean 0.5 K and max 12 K give SAFE 0.6, ALERT 0.4 (SAFE: the max alone never decides). The weights
   (`RuleConfig.mean_weight`, `max_weight`) are hypotheses; `tools/backtest_rules.py` re-rates stored slots with them,
   and the effect over the first day is under "Re-rating: the strongest view".

   With the trapezoids and the 0.05 safety margin the status changes at ΔT_eff = 1.95 K (SAFE/WARNING) and 6.95 K
   (WARNING/ALERT). Air 2.5σ drier than normal lowers both by 0.85 K; the midday sun raises both by up to 1 K
   (reflected sunlight lifts the 3.9 µm channel). Neither shift exceeds 1 K, so dry air alone never makes ALERT.
   The record's `adjust` keeps ΔT, both shifts and ΔT_eff (and, with two views, `delta_t_max_k`,
   `delta_t_max_eff_k`, `max_validity` and the max's share `max_weight`), and the reason says them in words. The old
   humidity weights (0.6/0.4) and the gate are gone.
9. **Dryness from one source** (`ingest.py`, `geo.py`). The normal is NASA POWER's own climatology: humidity per
   region, UTC hour and ±15 calendar days over the two years before (five until 17 October 2026: a candidate for
   recent land cover, still under evaluation; 2 x 31 days keeps at least 30 samples per hour), built from the same requests as the skin
   temperature baseline. "Before" means the same dates one and two years earlier, counted back from the table's
   first day, and a value counts only if it is at least half a year older than the hour it rates (`EARLIER` in
   `canonical.py`). Until 18 October 2026 the history was picked by calendar year, which broke across the new
   year: a late-December window lost the January half of last season, in January the current December counted as
   its own normal (pulling dryness towards 0), and 27-31 December had no normal at all once the year had turned.
   Each published POWER hour gets `dry_z = (normal − humidity) / max(σ, 3 %)` against the
   normal of its own hour (so the daily cycle is not an anomaly), and the region's `power.dry_z` is the mean of the
   last 24 published hours. POWER is 2-3 days late, which suits a signal that lasts days. While POWER's dryness is
   older than 4 days, or POWER has no usable humidity in those hours, this hour's Open-Meteo humidity against
   POWER's normal is the fallback, flagged
   `dryness_fallback:open_meteo` (two models, so a fixed offset between them may shift the curves).
10. **Storage.** Firestore database `asapify`, see the top of `repo.py` for every collection. Slot records live in
   `engine_regions/{id}/slots/{YYYYMMDDHHMM}`; POWER hours (support only) stay in `hours`; the map's day archive
   lives in `engine_days/{YYYY-MM-DD}` (below).
11. **Agent output.** `status confirmed / uncertain / negative` became the design's YAML schema with English keys:
   `validation_status` (CONFIRMED / REJECTED / NEEDS_MANUAL_REVIEW), `confidence_score`, `analysis`,
   `supporting_data`, `visual_evidence` and `draft_operator_alert`. The verdict still has no level field: the agent
   cannot change it.

## Two cycles on the private worker

**Geostationary, every 15 minutes:** `POST /_internal/engine/geo-tick?budget=240` (Cloud Scheduler job
`asapify-geo-tick`, minutes 5, 20, 35, 50). Both satellites scan every 10 minutes, so about one slot in three is
skipped and the day archive has gaps: smouldering peat changes over days, not minutes.

1. Agent runs that waited from an earlier cycle go first.
2. The slot: the newest one both satellites published (Himawari lands about 12 minutes after the scan, GK2A about
   22-24 minutes, measured on 13 Oct), or a slot older than 40 minutes that only one of them published. HEAD
   requests only; a processed slot is never read again (`engine_meta/ingestion.geo_slot`).
3. Each satellite: download to a temporary folder (Himawari 4 files, ~11 MB: segments 5-6 of B07 and B13; GK2A
   2 full-disk files, ~60 MB), decode with satpy, score all 65 regions. A broken file only drops that satellite.
4. Per region: fusion, the dryness (POWER, else the Open-Meteo fallback) and the sun, then the rules, then the state
   machine. Humidity is cached per hour in meta;
   FIRMS is asked once, and only when some region has a hot pixel. One Firestore batch writes every state, slot
   record and transition, and the slot's entry in the day archive.
5. A region that entered ALERT gets `last_agent_at` saved first (so the 24 h cooldown holds even if the agent fails)
   and an agent request in `engine_meta/ingestion.agent_pending`. Requests run while at least 60 s of budget is left;
   the rest wait for the next cycle.

**NASA POWER support, every 15 minutes:** `POST /_internal/engine/tick?batch=20&budget=240` (job
`asapify-engine-tick`). One request asks POWER for its latest published hour; most cycles stop there (one Firestore
read). New hours are written per region to `hours` with their skin-temperature anomaly and `dry_z`, and summarised
as `power` on the region document: the latest hour, `dry_z` (mean of the last 24 published hours), `dry_z_hours` and
`rh_normal` (that day's humidity normal per UTC hour for the fallback, as two lists of 24, `mean` and `std`:
Firestore stores no array inside an array). The baseline table in `engine_baselines` holds both normals (`entries`,
`entries_rh`); a table built before 16 October without `entries_rh` is rebuilt once, and a `power` summary without
`rh_normal` is rewritten on the next tick, without waiting for POWER's next hours.

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

Done with the rules of that day (night-only ALERT, the smaller ΔT of two agreeing satellites). The rules and fusion of
16 October have not been validated against VIIRS yet; `tools/backtest_rules.py` sets them beside the stored statuses
(for the second attribute of 17 October see the next section).

Night slot, both satellites decoded by this code and compared with NASA FIRMS VIIRS (NOAA-20, NOAA-21, S-NPP;
nominal/high confidence within 90 minutes; 12,463 detections over Sumatra and Kalimantan):

| | Himawari-9 | GK2A |
|---|---|---|
| Regions with a verdict (enough clear pixels) | 49 / 65 | 52 / 65 |
| Hot regions with a VIIRS fire within 1.4 km of the hot pixel | 10 | 10 |
| Hot regions without VIIRS | 1 (R0.50S101.875E) | 0 |
| Regions with VIIRS fires but no hot pixel (small fires, cloud) | 8 | 8 |

Both satellites were hot within 0.4-5.9 km of each other in the same 10 regions, all with VIIRS fires. The one
region only Himawari saw (55 km from GK2A's warmest pixel, no VIIRS) came out DEGRADED, capped at WARNING by those rules. The
strongest anomaly was R3.50S105.625E (South Sumatra): 39 K (Himawari) and 42 K (GK2A), 604 VIIRS detections in the
cell. Two choices come from this slot. The first version (ΔBT only, peat pixels only) had 6 of its 11 hot Himawari
regions without any VIIRS fire nearby (thin cirrus); the 3.9 µm test removed all 6. Scoring the whole cell instead
of the peat pixels alone then found 10 regions with a hot pixel next to a VIIRS fire instead of 4, because most fire pixels sat
just outside the simplified KHG polygons. One slot is not a calibration, and this was the only one done before the
freeze: treat every number as a hypothesis.

## Re-rating: the strongest view, 16-17 October 2026

How much the second attribute (item 8, BE #20) changes: every slot record stored from 16 Oct 2026 15:07 UTC to
17 Oct 15:07 UTC was fused again and rated twice with today's rules and the same dryness, once with the mean only
and once with the mean and the strongest view. Read-only, run by the team on 17 October with ADC.

| Mean only \ two attributes | SAFE | WARNING | ALERT | NO_OBSERVABLE |
|---|---|---|---|---|
| SAFE | 2,580 | 3 | 0 | 0 |
| WARNING | 0 | 207 | 23 | 0 |
| ALERT | 0 | 0 | 653 | 0 |
| NO_OBSERVABLE | 0 | 0 | 0 | 1,474 |

- 26 of 4,940 slots changed (0.75 % of the 3,466 rated ones), each one step up: 23 WARNING to ALERT, 3 SAFE to
  WARNING, in 20 regions. None went down and clouds were untouched, as the formula intends.
- In about 20 of the 26 the strongest view was about twice the mean: one satellite hot, the other close to 0 K.
  That is the case the attribute was added for (a hotspot one satellite sees is no longer averaged away), and also
  the one most open to a single-satellite false alarm. In the other 6 both satellites were hot and the mean, once
  shifted, sat just under the WARNING/ALERT boundary (5.8-7.4 K measured).
- Stored VIIRS support (a detection within 10 km and 12 hours of the hot pixel) was there in 4 of the 26 slots, POWER
  skin-temperature support in 3; 19 had neither. Most changes fell between 00 and 10 UTC (day in Indonesia), and
  VIIRS passes only a few times a day.

So the second attribute is rare and only ever raises a status by one step, but most of the ALERTs it adds rest on
one satellite without VIIRS. This compares two rule sets with each other, not with fires: the weights stay
hypotheses until such ALERTs are checked against later VIIRS detections.

## Cost and limits (billing guardrails)

- **Cloud Run worker** (1 vCPU, 1 GiB since 16 Oct, timeout 300 s): measured on one Linux vCPU with this slot, both satellites
  decode in about 18-21 s CPU with a peak of about 200 MB (dask chunks of 4 MiB; with dask's 128 MiB default it was
  ~50 s and 740 MB). With the downloads a tick takes roughly 25-35 s: 96 ticks a day (every 15 minutes) is about
  75-100k vCPU-seconds a month, inside Cloud Run's monthly free tier (180k vCPU-s, 360k GiB-s) if the rest stays
  small. **In production 512 MiB was not always enough:** in the 24 hours to 16 Oct 2026 11:00 UTC, 17 of 144 geo
  ticks (then every 10 minutes) returned 503 because the instance passed its memory limit (Cloud Run log "Memory
  limit ... exceeded"). On 16 Oct the worker was raised to 1 GiB with
  `gcloud run services update asapify-notify --memory 1Gi --region asia-southeast2 --project hack2skill-510921`
  (revision `asapify-notify-00021`; at 96 ticks a day about 75-100k GiB-seconds a month, still inside the free
  tier). The deploy pipeline only swaps the image, so the setting stays.
- **Downloads** come from NOAA's public buckets on AWS (no key, no egress fee); ingress to Cloud Run is free.
- **Firestore** (`asapify` holds the project's free tier): about 65 slot records + 65 state updates per slot,
  ~12.5k writes a day at 96 slots, plus ~1.6k for POWER: under the free 20k writes a day.
  Slot records carry `expire_at` for a TTL policy that drops them after 30 days (TTL deletes are billed, also cents).
  The region page reads up to 4 slot records per hour shown (24 h by default, at most 72 h; one day by date = at most
  96). The day archive adds one write per slot (96 a day); the map reads one document per day it opens and
  at most 33 per calendar month shown. A day is at most 1 MiB and is kept without TTL: under 365 MiB a year in the
  worst case, against Firestore's free 1 GiB.
- **Open-Meteo**: one request with 65 locations per hour, about 1,560 of the 10,000 free calls a day.
- **FIRMS**: one request per source per slot, only when a region has a hot pixel.
- **Gemini**: `AGENT_DAILY_QUOTA` (20 a day) taken in a transaction before any model call. Each run now carries two
  GIBS images (about 100-200 KB each, a few hundred input tokens) and one Open-Meteo request for the hot pixel.
- **NASA GIBS**: public, no key, no charge; two WMS requests per agent run.

## Verification agent

- `asapify_agent/agent.py` defines `root_agent` (ADK `Agent`, name `geospatial_analyst`) with the instruction from
  design section 8 and one tool, `fetch_firms_data`. The tool reads the region from the session state, so the model
  cannot query another area; it may only widen the time window (6-48 h) and the buffer (0-25 km). Each hotspot it
  returns carries `distance_km` (to the region centre) and, since 17 October 2026, `distance_hot_km` (to the hot pixel
  under review), which the confidence scale refers to.
- The payload is the slot record: status, utilities, ΔT, both satellites' views, cloud, humidity, day or night, the
  VIIRS/POWER support and the flags, plus what the attached images are and the weather at the hot pixel.
- Triggers: automatic on the transition into ALERT, and the operator's Re-verify button
  (`POST /api/v1/operator/regions/{id}/verify`, at most once per region per 5 minutes). An automatic request that
  meets a used-up daily quota stays queued in `engine_meta/ingestion.agent_pending`, in order, and runs once the
  quota resets (00:00 UTC); before 17 October 2026 it was dropped, so a region entering ALERT late in the UTC day could
  go 24 hours without a verification. Any other failure is still recorded, not retried.
- Limits in code and in the database, not only in the prompt: a hard daily quota taken in a Firestore transaction
  before any model call (`AGENT_DAILY_QUOTA`, default 20, per UTC day: it resets at 00:00 UTC = 07:00 UTC+7), at
  most 3 tool calls and 4 model calls per run,
  1,280 output tokens per call, minimal thinking, a 44 s timeout, at most 15 hotspots shown to the model.
- The YAML is parsed into `AgentVerdict`, then checked against what the tool really returned: the hotspot count and
  the highest brightness temperature must come from a tool result, and CONFIRMED needs at least one nominal- or
  high-confidence hotspot. Anything else is stored as NEEDS_MANUAL_REVIEW with the reason in `checks`.
- **Confidence** (`confidence_score`, 0-1) is the model's own number, not computed by the backend, which only checks
  its range. Since 17 October 2026 the instruction gives it a scale (rule 10): 0.85-1 several nominal- or
  high-confidence hotspots within a few km and hours of the hot pixel and nothing contradicting them, 0.6-0.85 farther,
  hours apart or mixed, 0.3-0.6 thin or partly contradictory, below 0.3 almost none; a REJECTED rarely above 0.7,
  because no hotspot is not proof. The region page shows it beside the FIRMS numbers the backend did check. The VIIRS
  I-4 channel saturates at about 367 K, so a `highest_brightness_temp` of 367 K is a pixel at the sensor's limit, not
  a fire temperature.
- Each verification records `location`: the point it is about (the slot's hot pixel, else its strongest fire-like
  pixel, else the cell centre); the weather is fetched there and the Telegram message names it (NOTIFICATIONS.md).
- **Visual check** (`imagery.py`, since 16 October 2026). Before the model runs, the backend fetches two NASA GIBS
  WMS images of the region plus about 11 km: VIIRS NOAA-20 true colour (smoke) and false colour M11-I2-I1 (burn
  scars, active fire, through thin smoke), both with the VIIRS thermal anomalies drawn on top, from the last daytime
  overpass GIBS can serve (about 13:30 local solar time, taken as served 4 hours later; a night observation gets the
  afternoon before it, and `hours_before_observation` says how long before). It also asks Open-Meteo for the wind,
  humidity and last 24 hours of rain at the hot pixel. Images and weather go to the model with the payload; the model
  reports `visual_evidence` (`smoke` and `burn_scar`: visible / not_visible / obscured / no_image, `image_date`,
  `notes`). The backend stores the image URLs, so the operator sees the same pictures. A picture that is not a JPEG
  or PNG, or is so small it is one flat colour (no data yet), is left out and named in `imagery.errors`. The visual
  report can only hold back a CONFIRMED (a REJECTED stays rejected, so it cannot become publishable): a report that
  does not name the image date, says `no_image` for a view that was shown, claims to see something without an image,
  or is missing although images were shown makes it NEEDS_MANUAL_REVIEW, and so does a CONFIRMED when both views,
  clear and within 24 hours, show neither smoke nor a burn scar although a confident hotspot was already there at
  the pass (up to an hour after it). An image never confirms a fire on its own. Images and weather get 10 s of wall
  clock together (whatever is late is left out), the model run 44 s: both fit the operator's 60 s request. GIBS
  layer names follow its catalogue and could not be tested from the development environment; check the first live
  verification's `imagery.errors`.
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
`gcp/geo-setup.sh` creates or updates the `asapify-geo-tick` job (every 15 minutes since 16 October; rerun it with
`APPLY=1` after deploying) and the TTL policy on `slots.expire_at`. The worker needs a 300 s request timeout for
`budget=240`. The visual check needs no new variable or key.

## Tests

`python -m pytest -q` runs everything offline: synthetic brightness-temperature grids for the contextual test,
fake satellites, FIRMS and Open-Meteo for the cycle, fake POWER fetchers, an in-memory `Repo`, a mock FIRMS
transport, fake GIBS and Open-Meteo answers for the visual check (`tests/conftest.py` keeps every test offline) and
a scripted model that drives the real ADK runner, images included. Decoding real files is checked by hand against
VIIRS (see Validation); the satellite libraries are only imported when a slot is read. `tools/backtest_rules.py`
(read-only, Cloud Shell) re-rates the stored slots with the current rules and prints old against new.
