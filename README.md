# ASAPify backend

Risk engine and operator API for the `KRL-TEAM-PROJECT/ktp-asapify-fe` frontend, deployed to Cloud Run in Jakarta.

> **Frozen on 16 October 2026.** Development has stopped; this repository and its docs describe the system as it
> stands. Only fixes that keep the deployed service running belong here. Known limitations are listed below.
> The tag `freeze-2026-10-16` marks the submitted state of `main`; the live image is `gh-f40381a`.

## Notifications

Alerts go to the team's Telegram group: the ASAPify bot posts every alert an operator publishes there, and anyone
who wants the information joins the group from the app's public join page. A Firestore outbox, Cloud Tasks with OIDC
and a recovery sweep carry each message. A fresh deploy starts in `dry_run`; production runs `live` since
`gcp/telegram-live.sh`. See [NOTIFICATIONS.md](docs/NOTIFICATIONS.md).
There are no personal subscriptions, inboxes or push messages.

## State at the freeze, 16 October 2026

| Component | Status |
|---|---|
| FastAPI `/api/v1`: health, regions, day archive, agent, decisions, operator access, notifications | Implemented; reads the engine's Firestore documents. The pixel grid / night / cluster API (15 Oct) and the never-built alert list/close endpoints (16 Oct) were removed |
| Authentication | Firebase ID token, verified email, custom claim `role=operator`; revocation checked |
| Public read-only | `/api/v1/public/regions`, `/regions/{id}` (24 h at most), `/days`, `/days/{date}`: the map, region detail and day archive for everyone, without operator emails or decision notes, and without actions. Cached by the Hosting CDN (`s-maxage` 120 s, past days 1 day) so public traffic barely reaches Firestore |
| Operator decision | Firestore transaction: decision, alert reservation, audit; a second decision is refused |
| Telegram | The bot posts every published alert in the team's Telegram group(s); an operator links a group from the Admin page. No personal chats. Live: published alerts reach the linked group |
| Cloud Run + GCS + Firestore + Artifact Registry | Live in `hack2skill-510921` since 9 Oct. Every merge to `main` that touches the API is tested and deployed by GitHub Actions (see [CLOUDSHELL.md](docs/CLOUDSHELL.md)). Firestore rules released 13 Oct (no direct browser access) |
| Risk engine | Live: 65 peat regions, Himawari-9 + GK2A every 10 minutes (the two satellites confirm each other; ALERT only at night with both agreeing and with a high ΔT), VIIRS + NASA POWER as support, Fuzzy MAUT, state machine, audit record per slot, hot pixels and nearby VIIRS per region, a day archive for the map's date picker. See [ENGINE.md](docs/ENGINE.md) |
| Himawari/GK2A decoding | In the engine (`asapify_engine/satellites.py`, satpy), checked against VIIRS on one night slot; the older `pipeline/` code was removed on 15 Oct and stays in git history ([ENGINE.md](docs/ENGINE.md) maps one to the other) |
| ADK/Gemini agent | `geospatial_analyst` on Vertex AI with `fetch_firms_data` (NASA FIRMS): automatic on entering ALERT and the Re-verify button, YAML checked before it is stored, hard daily quota |
| Cloud tests with real Firebase/Firestore/Telegram | Proven live: Google sign-in, 401/403, the private worker, satellite data on the map, and an operator publishing an alert that reached the Telegram group |

The engine covers **Sumatra + Kalimantan**: 65 peat regions, one 10-minute slot each, grouped by WIB day.

Statuses are `SAFE` < `WARNING` < `ALERT`, plus `NO_OBSERVABLE` (compact codes `S`, `W`, `A`, `N`).

## Known limitations at the freeze

- **Not calibrated.** Every threshold and weight in `RuleConfig`, `FusionConfig` and `ContextConfig` is an initial
  hypothesis, checked against VIIRS on one night slot only (ENGINE.md, Validation). A utility is a match score and
  the agent's confidence its own score; neither is a fire probability.
- **Not an operational early-warning system.** Latency and reach in the field were never measured; Telegram's
  `accepted` means the provider took the message, not that anyone read it, and delivery is not exactly-once.
- **Scope.** Sumatra and Kalimantan only; night streaks, neighbour support and a per-pixel history are not used.
- **History.** Slots stored before a rule change keep the result they had; nothing is recomputed (the ALERT
  heat requirement applies from its deployment on).
- **Agent.** NASA FIRMS hotspots are its only evidence (no imagery), with a hard quota of 20 runs a day.
- **Gaps in the day archive.** Until 16 Oct the worker `asapify-notify` ran with 512 MiB: in the 24 hours to
  16 Oct 11:00 UTC, 17 of 144 geo ticks returned 503 after the instance passed that limit, and a failed tick can
  leave its slot out of the archive (15 Oct holds 86 of 144 slots). The worker was raised to 1 GiB on 16 Oct
  (revision `asapify-notify-00021`); days before that keep their gaps. See ENGINE.md (Cost and limits).

## Local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
uvicorn main:app --host 127.0.0.1 --port 8080
```

`GET /api/v1/health` works without the cloud. Operator endpoints refuse requests without a token.
There is no login bypass, automatic demo seed or synthetic data in this service.
Missing data returns `DATA_NOT_READY`, never a `SAFE` status.

Use the [GCP guide](docs/GCP.md) for deployment and [ENGINE.md](docs/ENGINE.md) for the engine. The image
installs `requirements-api.txt`; `tools/build_regions.py` rebuilds the regions from the KHG peat shapefile in
`tools/khg/`.
