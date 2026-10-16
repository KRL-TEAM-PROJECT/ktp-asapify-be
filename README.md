# ASAPify backend

Operator API for the `KRL-TEAM-PROJECT/ktp-asapify-fe` frontend, deployed to Cloud Run in Jakarta.
Shipping this API does not mean the satellite detection system is operational.

## Notification Engine v1 — 9 October 2026

Alerts go to the team's Telegram group: the ASAPify bot posts every alert an operator publishes there, and anyone
who wants the information joins the group from the app's public join page. A Firestore outbox, Cloud Tasks with OIDC
and a recovery sweep carry each message; the default mode is `dry_run`. See [NOTIFICATIONS.md](docs/NOTIFICATIONS.md).
There are no personal subscriptions, inboxes or push messages.

## Integration status, 13 October 2026

| Component | Status |
|---|---|
| FastAPI `/api/v1`: health, regions, day archive, agent, decisions, operator access, notifications | Implemented; reads the engine's Firestore documents. The pixel grid / night / cluster API was removed on 15 Oct |
| Authentication | Firebase ID token, verified email, custom claim `role=operator`; revocation checked |
| Public read-only | `/api/v1/public/regions`, `/regions/{id}` (24 h at most), `/days`, `/days/{date}`: the map, region detail and day archive for everyone, without operator emails or decision notes, and without actions. Cached by the Hosting CDN (`s-maxage` 120 s, past days 1 day) so public traffic barely reaches Firestore |
| Operator decision | Firestore transaction: decision, alert reservation, audit; a second decision is refused |
| Telegram | The bot posts every published alert in the team's Telegram group(s); an operator links a group from the Regions page. No personal chats. `dry_run` until `gcp/telegram-live.sh` |
| Cloud Run + GCS + Firestore + Artifact Registry | Live in `hack2skill-510921` since 9 Oct. Every merge to `main` that touches the API is tested and deployed by GitHub Actions (see [CLOUDSHELL.md](docs/CLOUDSHELL.md)). Firestore rules released 13 Oct (no direct browser access) |
| Risk engine | 65 peat regions, Himawari-9 + GK2A every 10 minutes (the two satellites confirm each other; ALERT only at night with both agreeing), VIIRS + NASA POWER as support, Fuzzy MAUT, state machine, audit record per slot, hot pixels and nearby VIIRS per region, a day archive for the map's date picker. See [ENGINE.md](docs/ENGINE.md) |
| Himawari/GK2A decoding | In the engine (`asapify_engine/satellites.py`, satpy), checked against VIIRS on one night slot; the older `pipeline/` code was removed on 15 Oct and stays in git history ([ENGINE.md](docs/ENGINE.md) maps one to the other) |
| ADK/Gemini agent | `geospatial_analyst` on Vertex AI with `fetch_firms_data` (NASA FIRMS): automatic on entering ALERT and the Re-verify button, YAML checked before it is stored, hard daily quota |
| Cloud tests with real Firebase/Firestore/Telegram | Google sign-in proven live; 401/403 and the private worker verified. Publish, Telegram and the operator flow not tested yet (no data, bot not installed) |

The engine covers **Sumatra + Kalimantan**: 65 peat regions, one 10-minute slot each, grouped by WIB day.

Statuses are `SAFE` < `WARNING` < `ALERT`, plus `NO_OBSERVABLE` (compact codes `S`, `W`, `A`, `N`).

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
