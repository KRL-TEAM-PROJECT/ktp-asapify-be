# Deploying ASAPify to Google Cloud

This guide is for a dedicated ASAPify project that is active and linked to billing.
The `hack2skill-510921` project has been live since 9 October 2026; see [CLOUDSHELL.md](CLOUDSHELL.md) for its status.

To run every stage at once from the user's Cloud Shell account, see the
[Cloud Shell runner](CLOUDSHELL.md). The runner provides the plan, backend deploy, Hosting deploy and smoke checks.

## What gets created

| Resource | Configuration |
|---|---|
| Cloud Run | `asapify-api`, Jakarta `asia-southeast2`, 1 vCPU, 512 MiB, min 0/max 2 |
| Firestore Native | Database named `asapify`, Jakarta, delete protection |
| GCS | `<PROJECT_ID>-asapify-data`, uniform IAM, public access prevention |
| Build staging/logs | `<PROJECT_ID>-asapify-builds`, private |
| Artifact Registry | Docker repository `asapify`, Jakarta |
| Runtime account | `asapify-api`: datastore.user + firebaseauth.viewer, read-only data bucket |
| Builder account | `asapify-build`: objectAdmin on the build bucket only, writer on the image repository only |

The datastore.user role applies to the whole project, so use a project dedicated to ASAPify.
The scripts create no service-account JSON key, no Owner/Editor runtime account, no member invitation,
no new billing, VM, Redis or ingest cron. Schedulers/agents wait for a validated pipeline.

## 1. Choose the project and check costs

Open Cloud Shell in the right project. The setup account needs permission to create resources, enable APIs,
add IAM bindings, and `iam.serviceAccounts.actAs` on both service accounts. Run it as an authorised project
admin; the scripts never grant admin rights to other users.

Make sure billing is active, then create a budget and notifications at the threshold agreed with Harris.
A budget alert is not an automatic spending cap; min 0/max 2 does not guarantee zero cost either.
There is no live cost estimate yet: pixel count, satellite data volume and verification frequency are not measured.

From a checkout of the backend branch:

```bash
export ASAPIFY_PROJECT_ID='hack2skill-510921'
python3 gcp/manage.py check --project "$ASAPIFY_PROJECT_ID" --apply
python3 gcp/manage.py bootstrap --project "$ASAPIFY_PROJECT_ID"
```

The last command only prints the plan. Review the resources and the project; to create them:

```bash
python3 gcp/manage.py bootstrap --project "$ASAPIFY_PROJECT_ID" --apply
python3 gcp/manage.py build --project "$ASAPIFY_PROJECT_ID" --version operator-v1 --apply
python3 gcp/manage.py deploy --project "$ASAPIFY_PROJECT_ID" --version operator-v1 --apply
```

Bootstrap reads the inventory before creating anything; a permission failure is never treated as a missing resource.
Existing buckets are never deleted or emptied. If you reuse a bucket with the same name,
check its region and public access prevention yourself.
Use a new image version for every later change so rollback is unambiguous.

## 2. Enable Firebase Auth

Add Firebase to the **same GCP project** in the Firebase Console, create a Web App, enable the Google provider,
choose a support email, and add the Hosting domain to Authorized domains. The project owner handles the Firebase
account/consent steps. Copy the web configuration into the FE repository's `.env.local` following `.env.example`.
The web configuration is not an Admin key; never put a bot token or service-account key in `VITE_*`.

The first operator is set by hand. After that person signs in with Google once (refused until the role is granted),
an admin checks the email and UID in Firebase Authentication. From a Python environment authenticated with ADC:

```bash
gcloud auth application-default login
python tools/set_operator.py --project "$ASAPIFY_PROJECT_ID" --uid 'VERIFIED_UID'
python tools/set_operator.py --project "$ASAPIFY_PROJECT_ID" --uid 'VERIFIED_UID' --apply
```

No email from a chat gets access automatically. The operator signs out and in again to refresh the claim.

After that, operators add teammates on the Regions page (Operator access, `asapify_api/access_routes.py`):
- An existing, verified Google account becomes an operator at once.
- Any other address waits in `operator_access` and becomes an operator at its first sign-in; the web app calls
  `POST /api/v1/access/activate` and refreshes the token.
- Removing an address drops the claim and revokes that account's sessions. Nobody can remove their own access.
- Every change is written to `audit_log`.

The API's service account can already read accounts (`roles/firebaseauth.viewer`). Setting the claim and revoking
sessions also needs `firebaseauth.users.update`: `gcp/access-setup.sh` grants only that, through a custom role
(`APPLY=1` to run it).

## 3. Check the API before connecting Hosting

The first deploy uses private IAM. Use the gcloud proxy, which adds the cloud identity automatically:

```bash
gcloud run services proxy asapify-api --project "$ASAPIFY_PROJECT_ID" --region asia-southeast2 --port 8080
```

From another terminal: `curl -i http://127.0.0.1:8080/api/v1/health` must return 200;
`curl -i http://127.0.0.1:8080/api/v1/operator/regions` must return 401.
A valid Firebase token without the operator role must get 403. An expired/revoked token must get 401.
Check authorised access with an operator token from the app; never paste tokens into chats or commits.

For the Firebase Hosting rewrite, Cloud Run must accept requests at the transport layer without IAM.
Operator endpoints **still** verify the Firebase token and role on the server:

```bash
python3 gcp/manage.py expose-api --project "$ASAPIFY_PROJECT_ID"
python3 gcp/manage.py expose-api --project "$ASAPIFY_PROJECT_ID" --apply
```

That command gives `allUsers` only `roles/run.invoker`, not database/storage access.
If organisation policy forbids public invokers, do not weaken the policy: use an access design
approved by an admin. Hosting deployment is described in the FE repository's `docs/GCP.md`.

## 4. Connect data and notifications

The engine writes its own data: Cloud Scheduler calls the worker's geo tick every 10 minutes
(`gcp/geo-setup.sh`, see [ENGINE.md](ENGINE.md)).

The old inline Telegram path was replaced by a multi-channel outbox. Follow [NOTIFICATIONS.md](NOTIFICATIONS.md)
for the worker, Cloud Tasks, the Telegram group, the Firestore index and cloud tests.

## Remaining required work

1. Align the Sumatra/Kalimantan scope and night window decision with the team; record it in the worklog.
2. Calibrate the Himawari/GK2A thresholds against more VIIRS nights and daytime slots (one night checked, see ENGINE.md); add night streaks and neighbour support.
3. Complete the ADK/Gemini agent with VIIRS/imagery evidence and tool limits; drop the old path's ToPeCAl dependency.
4. Complete alert closing, outbox recovery, historical decision snapshots and signed image URL refresh.
5. Test real tokens, concurrent Firestore transactions, an alert in the Telegram group, the container build and the Firebase rewrite.
6. Only then add Cloud Scheduler for jobs that are runnable and idempotent.

Do not describe this stage as an operational early-warning system or a validated end-to-end satellite/AI demo.

## Official references checked

- https://docs.cloud.google.com/run/docs/deploying
- https://docs.cloud.google.com/sdk/gcloud/reference/builds/submit
- https://docs.cloud.google.com/build/docs/securing-builds/configure-user-specified-service-accounts
- https://firebase.google.com/docs/hosting/cloud-run
- https://firebase.google.com/docs/auth/admin/verify-id-tokens
- https://firebase.google.com/docs/auth/admin/custom-claims
- https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule
