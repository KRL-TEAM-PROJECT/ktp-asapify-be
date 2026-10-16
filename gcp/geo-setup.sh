#!/usr/bin/env bash
# Setup of the geostationary cycle (docs/ENGINE.md): the 15-minute Scheduler job on the private worker and
# the Firestore TTL policy that drops slot records 30 days after their slot. Prints the commands; APPLY=1 runs them.
# Before: an image with /_internal/engine/geo-tick is deployed. The job calls the worker with the same identity and
# audience as asapify-engine-tick, so no new account or role is needed.
set -euo pipefail
P=${ASAPIFY_PROJECT_ID:-hack2skill-510921}
R=${ASAPIFY_REGION:-asia-southeast2}
run() { if [ "${APPLY:-0}" = 1 ]; then "$@"; else printf '%q ' "$@"; echo; fi; }

read -r CALLER AUDIENCE < <(gcloud scheduler jobs describe asapify-engine-tick --project "$P" --location "$R" \
  --format='value(httpTarget.oidcToken.serviceAccountEmail,httpTarget.oidcToken.audience)')
[ -n "${AUDIENCE:-}" ] || { echo "asapify-engine-tick has no OIDC audience; set that job up first" >&2; exit 1; }
VERB=create
if gcloud scheduler jobs describe asapify-geo-tick --project "$P" --location "$R" >/dev/null 2>&1; then VERB=update; fi
# minutes 5, 15, ... 55: GK2A lands about 22-24 minutes after its scan, so each run finds a slot both satellites have
run gcloud scheduler jobs "$VERB" http asapify-geo-tick --project "$P" --location "$R" \
  --schedule='5-59/15 * * * *' --time-zone=Etc/UTC --http-method=POST \
  --uri="$AUDIENCE/_internal/engine/geo-tick?budget=240" \
  --oidc-service-account-email="$CALLER" --oidc-token-audience="$AUDIENCE" --attempt-deadline=300s
# slot records carry expire_at; TTL deletes are billed like deletes (cents a month at 65 records per slot)
run gcloud firestore fields ttls update expire_at --collection-group=slots --enable-ttl \
  --database=asapify --project "$P"
[ "${APPLY:-0}" = 1 ] || echo "# plan only: run again with APPLY=1"
