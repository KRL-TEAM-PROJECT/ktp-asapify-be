#!/usr/bin/env bash
# One-time IAM for operator access from the app (docs/GCP.md, section 2). Prints the commands; APPLY=1 runs them.
# asapify-api already has roles/firebaseauth.viewer (read accounts, check revoked tokens). Setting the operator claim
# and revoking sessions also needs firebaseauth.users.update, granted here through a one-permission custom role
# instead of roles/firebaseauth.admin, which could also create and delete accounts.
set -euo pipefail
P=${ASAPIFY_PROJECT_ID:-hack2skill-510921}
SA=asapify-api@$P.iam.gserviceaccount.com
run() { if [ "${APPLY:-0}" = 1 ]; then "$@"; else printf '%q ' "$@"; echo; fi; }

if ! gcloud iam roles describe asapifyClaimsEditor --project "$P" >/dev/null 2>&1; then
  run gcloud iam roles create asapifyClaimsEditor --project "$P" --title "ASAPify operator claims" \
    --description "Set the operator claim and revoke sessions from the operator access page" \
    --permissions firebaseauth.users.update --stage GA
fi
run gcloud projects add-iam-policy-binding "$P" --member "serviceAccount:$SA" \
  --role "projects/$P/roles/asapifyClaimsEditor" --condition None
[ "${APPLY:-0}" = 1 ] || echo "# plan only: run again with APPLY=1"
