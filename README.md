# ASAPify backend

Operator API for the `KRL-TEAM-PROJECT/ktp-asapify-fe` frontend, deployed to Cloud Run in Jakarta.
Shipping this API does not mean the satellite detection system is operational.

## Notification Engine v1 — 9 October 2026

Implements the multi-channel decision: inbox + regional consent, Telegram text, simulated/non-emergency push,
Cloud Tasks with OIDC, a Firestore outbox, acknowledgement and recovery. Default mode is `dry_run`.
See [implementation and activation details](docs/NOTIFICATIONS.md). WhatsApp, SMS, Gmail, LoRa and Bluetooth
are later stages and are never reported as delivered. Google Chat is not used.

## Integration status, 13 October 2026

| Component | Status |
|---|---|
| FastAPI `/api/v1`, health, metadata, grid, calendar, clusters | Implemented; reads pipeline output from GCS/Firestore |
| Authentication | Firebase ID token, verified email, custom claim `role=operator`; revocation checked |
| Operator decision | Firestore transaction: decision, alert reservation, audit; a second decision is refused |
| Telegram | Text adapter in the Notification Engine; DM linking, `dry_run` by default, accepted/unknown status |
| Cloud Run + GCS + Firestore + Artifact Registry | Live in `hack2skill-510921` since 9 Oct. Every merge to `main` that touches the API is tested and deployed by GitHub Actions (see [CLOUDSHELL.md](docs/CLOUDSHELL.md)). Firestore rules not released yet; data bucket still empty |
| Himawari/GK2A pipeline, baseline, MAUT, calibration | The older `pipeline/` code remains experimental; not connected to the new API |
| ADK/Gemini agent | Old scaffold only; `/verify` returns a clear 503, never a fake AI result |
| Alert list/close | Not implemented; endpoints return 503 and are not used by the UI yet |
| Cloud tests with real Firebase/Firestore/Telegram | Google sign-in, preferences and inbox proven live; 401/403 and the private worker verified. Publish, Telegram and the operator flow not tested yet (no data, bot not installed) |

The FE contract of 6 October covers **Sumatra + Kalimantan**, grid origin `[94.9, 6.2]`, step 0.02°,
54 night slots (20:00–04:50 UTC+7). It supersedes the 29 September worklog, which covered Kalimantan only
and phrased the night window differently. This service follows the FE contract for data exchange;
the detector scope decision still needs to be recorded with the team.

Pixel statuses are `SAFE`, `NO_OBSERVATION`, `WATCH` and `WARNING` (compact codes `S`, `N`, `W`, `A`).

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

Use the [GCP guide](docs/GCP.md) for deployment and the [storage contract](docs/DATA_CONTRACT.md)
to connect the observation/agent work. The older `requirements.txt` stays for pipeline experiments;
the API image uses `requirements-api.txt` so geospatial dependencies do not weigh down the operator service.
