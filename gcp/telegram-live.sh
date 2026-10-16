#!/usr/bin/env bash
# Switch Telegram delivery to live (docs/NOTIFICATIONS.md). Without APPLY=1 it only prints the plan.
# The bot token is read with a hidden prompt and goes straight into Secret Manager and a curl config on stdin:
# it never appears on a command line, in the output or in a log.
# Before: create the bot with @BotFather and the Telegram group the alerts go to. Messages are real after this.
# Undo: gcloud run services update <service> --update-env-vars NOTIFICATION_MODE=dry_run,TELEGRAM_ENABLED=false
set -euo pipefail
P=${ASAPIFY_PROJECT_ID:-hack2skill-510921}
R=${ASAPIFY_REGION:-asia-southeast2}
BOT=${TELEGRAM_BOT_USERNAME:?set TELEGRAM_BOT_USERNAME to the bot username, without @}
ORIGIN="https://$P.web.app"
API_SA="asapify-api@$P.iam.gserviceaccount.com"
WORKER_SA="asapify-notify@$P.iam.gserviceaccount.com"

if [ "${APPLY:-0}" != 1 ]; then
  cat <<PLAN
plan (APPLY=1 to run):
1. secrets telegram-bot-token (worker reads it) and telegram-webhook-secret (API reads it)
2. asapify-notify: TELEGRAM_BOT_TOKEN from the secret, NOTIFICATION_MODE=live, TELEGRAM_ENABLED=true
3. asapify-api: TELEGRAM_WEBHOOK_SECRET from the secret, TELEGRAM_BOT_USERNAME=$BOT, NOTIFICATION_MODE=live, TELEGRAM_ENABLED=true
4. setWebhook -> $ORIGIN/api/v1/notifications/telegram-webhook (messages only), then getWebhookInfo
PLAN
  exit 0
fi

put_secret() {  # name; value on stdin
  if gcloud secrets describe "$1" --project "$P" >/dev/null 2>&1; then
    gcloud secrets versions add "$1" --data-file=- --project "$P" >/dev/null
  else
    gcloud secrets create "$1" --data-file=- --project "$P" >/dev/null
  fi
}

read -rsp 'Telegram bot token: ' TOKEN; echo
[ -n "$TOKEN" ] || { echo "no token given" >&2; exit 1; }
HOOK=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 48)

# 1) secrets and who may read them: the worker sends, the API checks the webhook
printf '%s' "$TOKEN" | put_secret telegram-bot-token
printf '%s' "$HOOK" | put_secret telegram-webhook-secret
gcloud secrets add-iam-policy-binding telegram-bot-token --project "$P" \
  --member "serviceAccount:$WORKER_SA" --role roles/secretmanager.secretAccessor >/dev/null
gcloud secrets add-iam-policy-binding telegram-webhook-secret --project "$P" \
  --member "serviceAccount:$API_SA" --role roles/secretmanager.secretAccessor >/dev/null

# 2-3) services
gcloud run services update asapify-notify --region "$R" --project "$P" \
  --update-secrets TELEGRAM_BOT_TOKEN=telegram-bot-token:latest \
  --update-env-vars "NOTIFICATION_MODE=live,TELEGRAM_ENABLED=true,ASAPIFY_PUBLIC_ORIGIN=$ORIGIN"
gcloud run services update asapify-api --region "$R" --project "$P" \
  --update-secrets TELEGRAM_WEBHOOK_SECRET=telegram-webhook-secret:latest \
  --update-env-vars "NOTIFICATION_MODE=live,TELEGRAM_ENABLED=true,TELEGRAM_BOT_USERNAME=$BOT,ASAPIFY_PUBLIC_ORIGIN=$ORIGIN"

# 4) webhook with the shared secret; the token stays inside the curl config read from stdin
curl -sS -K - <<CFG | head -c 300; echo
url = "https://api.telegram.org/bot$TOKEN/setWebhook"
data = "url=$ORIGIN/api/v1/notifications/telegram-webhook"
data = "secret_token=$HOOK"
data-urlencode = "allowed_updates=[\"message\"]"
CFG
curl -sS -K - <<CFG | head -c 400; echo
url = "https://api.telegram.org/bot$TOKEN/getWebhookInfo"
CFG
unset TOKEN HOOK
echo "Telegram is live. On the Admin page open Telegram groups, add the bot to your group and paste its invite link, then publish one verified region."
