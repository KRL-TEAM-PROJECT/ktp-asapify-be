#!/usr/bin/env bash
# Operator self-registration for the hackathon judging period (asapify_api/access_routes.py). Prints the commands;
# APPLY=1 runs them. UNTIL=YYYY-MM-DD is the last day (WIB) an account can register; it also ends those accounts'
# access. Steps: enable reCAPTCHA Enterprise, let asapify-api create assessments, create a checkbox key for the app's
# domains (skipped when RECAPTCHA_SITE_KEY is given), then put the key and the date on asapify-api. The deploy
# pipeline only swaps the image, so these settings stay. To close early, run again with UNTIL=2020-01-01 (and the
# same RECAPTCHA_SITE_KEY), or remove OPERATOR_SIGNUP_UNTIL from the service.
set -euo pipefail
P=${ASAPIFY_PROJECT_ID:-hack2skill-510921}
R=${ASAPIFY_REGION:-asia-southeast2}
UNTIL=${UNTIL:?set UNTIL=YYYY-MM-DD, the last day (WIB) of the judging period}
DOMAINS=${DOMAINS:-$P.web.app,$P.firebaseapp.com,localhost}
run() { if [ "${APPLY:-0}" = 1 ]; then "$@"; else printf '%q ' "$@"; echo; fi; }
[[ "$UNTIL" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "UNTIL must look like 2026-10-31" >&2; exit 1; }

run gcloud services enable recaptchaenterprise.googleapis.com --project "$P"
run gcloud projects add-iam-policy-binding "$P" --condition=None \
  --member="serviceAccount:asapify-api@$P.iam.gserviceaccount.com" --role=roles/recaptchaenterprise.agent

KEY=${RECAPTCHA_SITE_KEY:-}
if [ -z "$KEY" ]; then
  CREATE=(gcloud recaptcha keys create --project "$P" --display-name="ASAPify operator signup" --web
          --integration-type=checkbox --domains="$DOMAINS" --format='value(name)')
  if [ "${APPLY:-0}" = 1 ]; then
    KEY=$("${CREATE[@]}"); KEY=${KEY##*/}   # projects/<p>/keys/<id> -> <id>
    echo "created site key $KEY (pass it as RECAPTCHA_SITE_KEY next time, so no second key is made)"
  else
    printf '%q ' "${CREATE[@]}"; echo; KEY='<new-key-id>'
  fi
fi
run gcloud run services update asapify-api --region "$R" --project "$P" \
  --update-env-vars "RECAPTCHA_SITE_KEY=$KEY,OPERATOR_SIGNUP_UNTIL=$UNTIL"
[ "${APPLY:-0}" = 1 ] || echo "# plan only: run again with APPLY=1"
