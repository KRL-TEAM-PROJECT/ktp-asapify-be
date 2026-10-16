# ASAPify alerts in Telegram

**Telegram works through one group (team decision, 15 October 2026).** The team keeps a Telegram group (for
example "ASAPify Notified"). An operator adds the ASAPify bot to it from the app, and from then on the bot posts
every alert an operator publishes there. Anyone who wants to know about fires joins the group from the app's public
join page (`/join`) and reads it. Nobody subscribes, links a personal chat or keeps an inbox in the app. The bot posts
alerts and answers only two things: `/start` in a group (whether the group is now linked) and any private message (a
pointer to `/join`). It answers no questions.

Status at the freeze (16 October 2026): live in `hack2skill-510921` (bot `@asapify_alert_bot`); published alerts
reach the linked group. The group's invite link was not saved yet on 16 Oct (`GET /api/v1/telegram/join` returned
`[]`), so the public join page shows no Join button until an operator does step 5 below.

## Flow and guarantees

1. The operator chooses Publish after the agent's verification. One Firestore transaction saves the decision, the
   audit entry and the alert (`fanout: pending`). After the commit, the API writes one job per linked group.
   Ids are deterministic and writes are `create`-only, so a retry or a concurrent run never duplicates a message or
   resets an already claimed job. No provider is called inside the transaction.
2. The API tries to enqueue. A failed fan-out or enqueue never loses the decision; the response says
   `queue.awaiting_recovery`. The Scheduler sweep resumes `fanout: pending` alerts, scans the `pending` outbox and
   retries. Task names use a hash and a generation.
3. Cloud Tasks calls the private worker with OIDC as the expected service account. Chat ids never go into a task.
4. A worker transaction claims one job and re-checks that the group is still linked and the alert still active and
   unexpired (one hour from publishing). A repeated or concurrent request that sees `sending` or a terminal result
   never sends again.
5. The worker posts plain text with `sendMessage` (no photo, no HTML, no buttons). Telegram's answer means
   `accepted`, not that a member read it.
6. A timeout or a dead process after the claim may mean Telegram already accepted it. The status becomes `unknown`;
   there is no automatic retry. The sweep marks `sending` older than 5 minutes as unknown. This avoids blind
   resends; it is **not an exactly-once guarantee**.

Job statuses: `pending`, `sending`, `accepted`, `failed`, `unknown`, `blocked`, `cancelled`, `simulated`.
`simulated` means Telegram was not contacted (`dry_run`). Operators see each group's status under the decision
(`GET /api/v1/operator/alerts/{id}/deliveries`).

## Setup

### Base services

Run the base setup from [GCP.md](GCP.md), including the `asapify` database. These scripts only print the plan without
`--apply`.

```bash
export ASAPIFY_PROJECT_ID='hack2skill-510921'
python3 -m gcp.notifications bootstrap --project "$ASAPIFY_PROJECT_ID"
python3 -m gcp.notifications bootstrap --project "$ASAPIFY_PROJECT_ID" --apply
python3 -m gcp.notifications deploy --project "$ASAPIFY_PROJECT_ID" --version notification-v1 \
  --public-origin "https://${ASAPIFY_PROJECT_ID}.web.app" --apply
```

The notification deploy script always restores `NOTIFICATION_MODE=dry_run`. The worker stays private at the IAM
layer. The `asapify-task` account gets `run.invoker` on the worker only; it has no database access. The API and worker
get `cloudtasks.enqueuer` and actAs on the task account only; the worker gets `datastore.user`. No Owner/Editor role
and no service-account key.

Deploy the index and rules from the `gcp` folder after checking the database's existing rules:

```bash
cd gcp
npx firebase-tools deploy --only firestore --config firebase.notifications.json --project "$ASAPIFY_PROJECT_ID"
```

The bundled rules deny every direct browser access to Firestore; the server uses IAM. **Never replace another
application's database rules.** The one index serves the outbox scan (`notification_jobs` by status and due time).

### The Telegram group

1. Create the bot with @BotFather (`/newbot`). Keep the token out of chats and tickets.
2. Create the Telegram group the alerts go to.
3. Run `gcp/telegram-live.sh` (plan first, then `APPLY=1`). It stores the token as the secret `telegram-bot-token`
   (worker only) and a random `telegram-webhook-secret` (API only), sets `TELEGRAM_BOT_USERNAME`,
   `TELEGRAM_ENABLED=true` and `NOTIFICATION_MODE=live` on both services, and points the bot's webhook at
   `/api/v1/notifications/telegram-webhook` with that secret (`allowed_updates=["message"]`).
4. An operator opens the Admin page, Telegram groups, "Add the bot to a group". The app gets a one-time
   `t.me/<bot>?startgroup=<code>` link (10 minutes, stored hashed, used once). Telegram asks which group, adds the
   bot and posts `/start@<bot> <code>` there; the webhook links that group and its title shows in the panel. Telegram
   does not always post that message (never when the bot is already in the group), so the panel also shows it to
   copy and send in the group by hand. The bot answers in the group: "Linked", or that the code is not valid or has
   expired. The bot's privacy mode can stay on: Telegram delivers that command to the bot either way. Each answer is
   logged as `telegram webhook` with an `outcome` (linked, already, no_code, bad_code, private, migrated) and never
   the code, chat id or text.
5. The operator pastes the group's invite link (Telegram: group, Invite via link, copy) into the panel. The public
   join page then shows a Join button for that group.

Only groups are linked, and only with an operator's code: adding the bot to another group, or writing to it
privately, links nothing. The chat id stays on the server; operators see the title and can unlink the group. When
Telegram turns the group into a supergroup, the link follows the new chat id.

Undo: `gcloud run services update <service> --update-env-vars NOTIFICATION_MODE=dry_run,TELEGRAM_ENABLED=false`
on `asapify-api` and `asapify-notify`.

### Not tested in the cloud

- Two identical Cloud Tasks jobs (one claim expected), and a worker stopped after a claim (`unknown` expected).
  The offline tests cover the single claim and `unknown` after a send timeout, not the sweep's 5-minute rule.
- Latency from publishing to the group was never measured; never report `accepted` as phone latency.

## API

| Endpoint | Access / purpose |
|---|---|
| GET `/api/v1/telegram/join` | Public; title and invite link of every group that has one |
| GET `/api/v1/operator/telegram-groups` | Operator; linked groups (title, invite link), never the chat id |
| POST `/api/v1/operator/telegram-groups/link` | Operator; one-time `startgroup` link, 10 minutes |
| PUT `/api/v1/operator/telegram-groups/{id}` | Operator; set or clear the invite link (`https://t.me/+…`, `t.me/joinchat/…` or a public `t.me/<name>`) |
| DELETE `/api/v1/operator/telegram-groups/{id}` | Operator; stop alerts to a group |
| GET `/api/v1/operator/alerts/{id}/deliveries` | Operator; each group's status, error and timestamps |
| POST `/api/v1/notifications/telegram-webhook` | Webhook secret; links a group with an operator's code and answers `/start` and private messages in the webhook response |
| POST `/_internal/notifications/dispatch` | Worker OIDC, body job_id |
| POST `/_internal/notifications/sweep` | Scheduler OIDC; queue/unknown recovery |

## Official references

- https://core.telegram.org/bots/api#sendmessage
- https://core.telegram.org/bots/api#setwebhook
- https://core.telegram.org/bots/features#deep-linking
- https://docs.cloud.google.com/tasks/docs/creating-http-target-tasks

The risk engine and the Gemini agent only hand verified regions to Telegram through an operator decision. This is not
an operational early-warning system and has not proven latency or reach in the field.
