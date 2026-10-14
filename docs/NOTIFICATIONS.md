# ASAPify Notification Engine v1

Implements the decision from the 9 October 2026 chat:
https://chatgpt.com/s/t_6ac82245a3988191a78613a3f6fc0751

Goals: several channel choices, consent per region, short messages, receipt confirmation,
a recoverable queue, and a delivery trail. Google Chat is not used.
Status: deployed to `hack2skill-510921` in `dry_run`; **no message has reached a real recipient yet**.

## Scope of this stage

| Component | Implementation |
|---|---|
| Cloud Run API, private worker, Firestore | API code, publish and outbox transactions, setup scripts |
| Cloud Tasks, Cloud Scheduler | OIDC HTTP tasks, deterministic names, outbox recovery every minute |
| Inbox | Subscriber Firebase sign-in; regions, consent, 50 latest messages, idempotent confirmation |
| Telegram | DM linking with a one-time code; webhook secret; lightweight text `sendMessage` |
| FCM and Web Push | One FCM transport; one device per account; exercise/non-emergency only |
| WhatsApp, SMS, Gmail | Stored choice and routing contract; adapters **not implemented** |
| LoRa / LoRaWAN, Bluetooth Mesh | Choice/design only; no driver, gateway or device yet |
| Offline | The frontend keeps the public shell and the inbox the user chose to save; no new messages while offline |
| Mass public use | Not load-tested; fan-out pages of 50 subscribers, no cap on the total |

The subscriber count never fails a decision. Fan-out runs after the decision is saved, 50 subscribers per
batch (≤ 400 writes, under Firestore's 500 limit), inside the publish request.
For thousands of subscribers per province, move fan-out to the worker and load-test first (Cloud Run timeout 60 s).

**FCM design correction:** Google's documentation rules out FCM for emergency/high-risk use cases.
So every operational ALERT publication gets `purpose=emergency` from the server and is blocked for
`fcm`/`web_push` at three layers: planning, claiming and the adapter. Editing the caption or priority does not
change the classification. Backtests get `replay=true` from the pipeline manifest and become `exercise`;
recipients must opt in to exercises. Never change replay to disguise a real warning as information.

## Flow and guarantees

1. The operator chooses Publish after verification. One Firestore transaction saves the decision, the audit entry
   and the alert (`fanout: pending`). After the commit, the API writes inbox entries and channel jobs for consenting
   subscribers, page by page. Ids are deterministic and writes are `create`-only, so a retry or a concurrent run never
   duplicates a message or resets an already claimed job. No provider is called inside the transaction.
2. The API tries to enqueue. A failed fan-out or enqueue never loses the decision; the response says
   `queue.awaiting_recovery`. The Scheduler resumes `fanout: pending` alerts, scans the `pending` outbox and retries.
   Task names use a hash and a generation.
3. Cloud Tasks calls the private worker with OIDC as the expected service account. The task name header alone
   grants nothing. UIDs, chat ids and device tokens never go into a task.
4. A worker transaction claims one job and re-checks current consent, region, expiry and acknowledgement.
   A repeated or concurrent request that sees `sending` or a terminal result never sends again.
5. A provider response means `accepted`, not `delivered`/`read`. The Confirm receipt button sets
   `acknowledged_at` on that account's inbox entry. Merely opening the link is not an acknowledgement.
6. A timeout or a dead process after the claim may mean the provider already accepted it. The status becomes `unknown`;
   there is no automatic provider retry. The Scheduler marks `sending` older than 5 minutes as unknown.
   This avoids blind resends; it is **not an exactly-once guarantee**. Operators/admins reconcile unknowns.
7. v1 delivery TTL = one hour from publishing; push TTL on the device = 5 minutes. The inbox keeps the history.
   Emergency SMS/WhatsApp is planned in parallel, without the 120-second wait. For exercises/information,
   escalation is planned 120 seconds later. Today those adapters return `blocked/ADAPTER_PENDING`.

One race cannot be undone: withdrawing consent or acknowledging after the worker started sending
may not stop the request in flight. Every later job re-checks the conditions.

Channel statuses: `pending`, `sending`, `accepted`, `failed`, `unknown`, `blocked`, `cancelled`, `simulated`.
`simulated` means no provider was contacted. Only `pending` jobs are scheduled; zero SMS/WhatsApp are active,
so v1 has no paid-message cost. Do not open a paid adapter without an atomic daily quota,
a per-recipient rate limit, approved templates, consent and delivery-receipt tests.

## Selected project

The user's GCP screenshot of 9 October 2026 shows the name `hack2skill`, project id
`hack2skill-510921` and project number `950889990251`. The non-secret profile is in `gcp/project.json`.
The Jakarta region is the team's design, not something the screenshot proves.

## Configuration and deployment

Run the base setup from [GCP.md](GCP.md), including the `asapify` database and Google sign-in in Firebase Auth.
Use a dedicated project approved by its owner, with billing enabled. Tokens/secrets stay in Secret Manager.
These scripts only print the plan without `--apply`.

```bash
export ASAPIFY_PROJECT_ID='hack2skill-510921'
python3 gcp/manage.py bootstrap --project "$ASAPIFY_PROJECT_ID"
python3 -m gcp.notifications bootstrap --project "$ASAPIFY_PROJECT_ID"
python3 gcp/manage.py bootstrap --project "$ASAPIFY_PROJECT_ID" --apply
python3 -m gcp.notifications bootstrap --project "$ASAPIFY_PROJECT_ID" --apply
python3 gcp/manage.py build --project "$ASAPIFY_PROJECT_ID" --version notification-v1 --apply
python3 gcp/manage.py deploy --project "$ASAPIFY_PROJECT_ID" --version notification-v1 --apply
python3 -m gcp.notifications deploy --project "$ASAPIFY_PROJECT_ID" --version notification-v1 \
  --public-origin "https://${ASAPIFY_PROJECT_ID}.web.app"
python3 -m gcp.notifications deploy --project "$ASAPIFY_PROJECT_ID" --version notification-v1 \
  --public-origin "https://${ASAPIFY_PROJECT_ID}.web.app" --apply
```

The notification deploy script always restores `NOTIFICATION_MODE=dry_run` and `FCM_ENABLED=false`.
Switch to live only after the agreed demo-recipient tests. The worker stays private at the IAM layer.
The `asapify-task` account gets `run.invoker` on the worker only; it has no database access.
The API and worker get `cloudtasks.enqueuer` and actAs on the task account only. The worker gets
`datastore.user` and `firebasecloudmessaging.admin`; no Owner/Editor role and no service-account key.
The default Cloud Tasks / Scheduler service agents must keep their built-in roles to mint OIDC tokens.

Deploy indexes and rules from the `gcp` folder after checking the database's existing rules:

```bash
cd gcp
npx firebase-tools deploy --only firestore --config firebase.notifications.json --project "$ASAPIFY_PROJECT_ID"
```

The bundled rules deny every direct browser access to Firestore; the server uses IAM. **Never
replace another application's database rules.** This configuration targets the dedicated `asapify` database.
Wait for the indexes to be READY before testing publish/inbox. Every configuration field is listed in `.env.example`.

### Telegram

- Create a demo bot; store its token as the secret `telegram-bot-token`, readable by the worker only.
- Create a random webhook secret `telegram-webhook-secret`, readable by the API only.
- Point the bot webhook at `https://<API_OR_HOSTING>/api/v1/notifications/telegram-webhook`
  with the same `secret_token` and `allowed_updates=["message"]` through the official Telegram API.
- Set `TELEGRAM_BOT_USERNAME` on the API. The UI creates a one-time /start deep link valid for 10 minutes.
- The subscriber signs in, picks regions and Telegram, consents, opens the bot and presses Start.
  Linking only accepts private chats from the webhook; a browser cannot set someone else's chat as a target.
- For a live demo, set `TELEGRAM_ENABLED=true` and `NOTIFICATION_MODE=live` on the API and the worker,
  after the worker has the token secret. Photos are optional, so messages never wait for an image upload.
- Provincial Telegram groups from the old design are not enabled. This stage uses one DM per recipient
  so consent and acknowledgement can be attributed correctly. Group targets need admin registration
  and separate group consent; not implemented.

### Browser push / FCM

In the Firebase Console, make sure the FCM API is on, then copy the Web Push VAPID public key and the
messaging sender id into the frontend's public configuration. The web configuration is not a service-account key.
Browsers need HTTPS, a service worker and notification permission. Click Register browser push, choose Web Push,
and save consent plus the exercise opt-in. Choosing FCM and Web Push together still makes one task per token.
Set `FCM_ENABLED=true` on the API and worker only for exercise/non-emergency tests. The code always blocks emergencies.

### Cloud tests still needed

- Real Firebase token: a subscriber can open their own inbox, but operator endpoints stay refused.
- Concurrent Firestore transactions and indexes: two operators publishing the same verification publish it once.
- Two identical Cloud Tasks jobs: one claim; stop the worker after a claim to check `unknown`.
- Withdraw consent/acknowledge before a job is due and confirm the cancellation; test expiry and fan-out
  above 50 subscribers (more than one page), including the API dying mid fan-out and the sweep resuming it.
- Telegram to a verified demo account and exercise push to a browser that allows it. Record
  published_at, started_at, finished_at, acknowledged_at; never report accepted as phone latency.
- Test phones in the target areas; SMS/WhatsApp cannot be compared before an adapter/provider exists.

## Additional API

| Endpoint | Access / purpose |
|---|---|
| GET `/api/v1/notifications/settings` | Subscriber; preferences without tokens or secret chat addresses |
| PUT `/api/v1/notifications/preferences` | Subscriber; provinces, channels, consent, exercises |
| POST / DELETE `/api/v1/notifications/device` | Subscriber; register / remove one push device |
| POST `/api/v1/notifications/telegram-link` | Subscriber; one-time deep link code |
| POST `/api/v1/notifications/telegram-webhook` | Webhook secret; binding only, never replies |
| GET `/api/v1/notifications/inbox` | The account's 50 latest messages |
| POST `/api/v1/notifications/inbox/{id}/ack` | Idempotent acknowledgement; another account's id -> 404 |
| GET `/api/v1/operator/alerts/{id}/deliveries` | Operator; status, error, timestamps, acknowledgement |
| POST `/_internal/notifications/dispatch` | Worker OIDC, body job_id |
| POST `/_internal/notifications/sweep` | Scheduler OIDC; queue/unknown recovery |

## Official references

- https://firebase.google.com/docs/cloud-messaging/customize-messages/setting-message-priority (life critical use cases)
- https://firebase.google.com/docs/cloud-messaging/js/client
- https://docs.cloud.google.com/tasks/docs/creating-http-target-tasks
- https://docs.cloud.google.com/tasks/docs/samples/cloud-tasks-create-http-task-with-token
- https://core.telegram.org/bots/api#sendmessage
- https://core.telegram.org/bots/api#setwebhook

The risk engine and the Gemini agent only hand verified regions to the notification engine through an operator decision. This package is not
an operational early-warning system and has not proven latency, reach or delivery rate in the field.
