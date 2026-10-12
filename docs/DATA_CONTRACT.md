# Pipeline output → operator API

The frontend contract is `ktp-asapify-fe/src/types.ts` and `src/contract/spec.ts`.
This API reads results that are already computed; it does not run detection. The experimental
`pipeline/` code does not produce this contract yet. No synthetic observation data is installed automatically.

## Bundle accepted by the publisher

`tools/publish_snapshot.py` accepts one JSON file containing:

| Key | Shape |
|---|---|
| `index` | `PixelIndex`: version, origin `[94.9,6.2]`, step 0.02, parallel rows/cols/province |
| `grid` | `GridCompact`: pixels_version, UTC slot, n_sat, status, base64 utility, clusters |
| `meta` | archive_first YYYY-MM-DD, himawari_slot/gk2a_slot UTC or null, explicit boolean replay |
| `night` | `NightCompact`: night, pixels_version, 54 slots/status/n_sat, clusters |
| `clusters` | List of `ClusterDetail`, plus `warning_nights` (list of night ids) |

`grid.pixel_n_sat` is an extra 0/1/2 list per pixel needed for GeoJSON. The FE compact format works
without it. The satellite count of a whole slot must never be claimed as the clear-sky satellite count of every pixel.

All times are `YYYY-MM-DDTHH:MM:SSZ` strings, ordered in UTC. Every number comes from a measured source.
Compact status codes: `S` = SAFE, `N` = NO_OBSERVATION, `W` = WATCH, `A` = WARNING. `N` always has a null
utility (byte 255); other values are 0..250. PixelIndex.version must match.
Night slots follow the FE: 13:00–21:50 UTC, 54 slots. Slots not evaluated yet are null.
`clusters` holds only rule-engine clusters, with attributes, series, neighbours and verification as typed in the FE.
All text fields are English (`caption`, `verification.summary`, `evidence[].finding`); there are no per-language variants.
`decision` must be null/absent from the pipeline; only the API writes decisions.
For replay, publish backtest output with `meta.replay=true`; backtest numbers are not automatic validation metrics.
Alerts published from replay data are `exercise` messages and only reach subscribers who opted in to exercises.

```bash
python tools/publish_snapshot.py /path/pipeline_output.json \
  --project "$ASAPIFY_PROJECT_ID" --bucket "$ASAPIFY_PROJECT_ID-asapify-data"
# After reviewing the validation, add --apply to store it.
```

Run it with ADC allowed to write GCS and Firestore. The API runtime account can only read the bucket, by design.
This publisher is a temporary integration boundary; converting ingested Parquet into the JSON bundle
is for the pipeline team to add.

## Storage

- GCS `api/pixels/<sha256>.json`, `api/grid/<sha256>.json`, `api/nights/<sha256>.json`: content-addressed,
  immutable objects. Raw/Parquet/crops may stay under the team's planned prefixes; the API never reads them directly.
- Firestore `api_snapshots/<slot>`: timestamp, n_sat, archive_first, replay, himawari_slot, gk2a_slot,
  pixels_object, grid_object. Queried on the single timestamp field; no composite index needed.
- `api_nights/<night>`: night, month, has_data, warning_clusters, object, as_of.
- `clusters/<id>`: current ClusterDetail + warning_nights + observation_at.
- `clusters/<id>/history/<slot>`: timestamp and the full cluster for `as_of` reads.
- `alerts/<id>`: publish decision and delivery status; `audit_log/<auto-id>`: who decided and when.
- `notification_recipients`, `notification_inbox`, `notification_jobs`, `notification_devices`,
  `notification_telegram_chats`: subscriptions, inbox, send jobs, push tokens and Telegram DM links.
  Written only by the API/worker; see [NOTIFICATIONS.md](NOTIFICATIONS.md). Provincial Telegram groups are not used.

The publisher never overwrites an operator decision and never replaces the current cluster with an older snapshot.
History must include every change that should be visible during replay. Decision snapshots between two slots,
historical closed state, peat-mask version rollover and expired image URLs need more work before replay
can serve as a complete audit.

Public metadata without a token contains no operator email or cluster detail. Every operator endpoint
requires a Firebase token. Never make the data bucket public; province/peat layers that are genuinely public
may ship as static FE assets after checking the source licence.
