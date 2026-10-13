#!/usr/bin/env bash
# One-time agent configuration for both Cloud Run services. The deploy pipeline only swaps the image, so these
# settings stay. Prints the commands; APPLY=1 runs them.
# Before: Vertex AI enabled, roles/aiplatform.user for asapify-api and asapify-notify, and secretAccessor on
# firms-map-key for both (docs/ENGINE.md). The key itself never appears here: Cloud Run reads it from Secret Manager.
set -euo pipefail
P=${ASAPIFY_PROJECT_ID:-hack2skill-510921}
R=${ASAPIFY_REGION:-asia-southeast2}
run() { if [ "${APPLY:-0}" = 1 ]; then "$@"; else printf '%q ' "$@"; echo; fi; }

for service in asapify-api asapify-notify; do
  run gcloud run services update "$service" --region "$R" --project "$P" \
    --update-secrets FIRMS_MAP_KEY=firms-map-key:latest \
    --update-env-vars GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_LOCATION=global,GEMINI_MODEL=gemini-3.5-flash,AGENT_DAILY_QUOTA=20
done
[ "${APPLY:-0}" = 1 ] || echo "# plan only: run again with APPLY=1"
