# Deploying ASAPify from Cloud Shell

Fixed target: project `hack2skill-510921`, Jakarta region `asia-southeast2`, database `asapify`.
The runner handles the API, worker, queue, indexes and the recovery scheduler, and always leaves notifications in
`dry_run`. The engine's schedule (`gcp/geo-setup.sh`), the agent (`gcp/agent-setup.sh`) and live Telegram
(`gcp/telegram-live.sh`) are separate scripts.

## History up to the freeze (16 October 2026)

- 9 Oct: Fitroh ran the `backend` stage and set up Hosting. The API, worker, queue, three indexes and the
  sweep schedule are live. The FE is deployed by GitHub Actions in the FE repo (see `docs/GCP.md` there).
- 12 Oct: the API and worker image was replaced with `main` `c55a14c` (tag `fix-20261012-113615`) using
  "Image-only update" below. Mode stayed `dry_run`; Telegram and FCM stayed off.
- 13 Oct: automatic deployment is active (see "Automatic deployment" below); first run deployed `gh-dfc52e1`.
  Firestore rules released (no direct browser access).
- 16 Oct, freeze: the engine fills the map every 10 minutes, Telegram is live and published alerts reach the linked
  group. The GCS data bucket stays empty: no code uses it. Both services run `gh-f40381a` (tag
  `freeze-2026-10-16`). Artifact Registry keeps the latest 3 images and deletes older ones after 7 days.
- After the freeze tag (16 Oct): adaptive fuzzy curves, the visual check and a 15-minute cycle. After the deploy
  that brings them, rerun `gcp/geo-setup.sh` with `APPLY=1` so the geo tick runs every 15 minutes.

## 1. Open Cloud Shell and get the source

In the Google Cloud Console, select `hack2skill-510921` and open Cloud Shell. Use an account allowed to
manage the project and accept the Cloud Shell authorisation when asked. Billing must already be active;
the runner never creates a billing account or changes budgets. The resources it creates may cost money.

Clone both private repositories with a GitHub account that has access. If `gh` is available, sign in with
`gh auth login --hostname github.com --git-protocol https --web`, then run `gh auth setup-git`.
Never put a GitHub token, sign-in code, password or service-account key in a chat or a commit.

Run from a new directory so an existing working checkout is not overwritten:

```bash
git clone https://github.com/KRL-TEAM-PROJECT/ktp-asapify-be.git
git clone https://github.com/KRL-TEAM-PROJECT/ktp-asapify-fe.git
cd ktp-asapify-be
```

Deploy only reviewed `main`. Record `git rev-parse HEAD` in both checkouts before deploying.

## 2. Deploy the backend

```bash
python3 -m gcp.cloudshell backend
python3 -m gcp.cloudshell backend --apply
```

Without `--apply`, the runner only prints the plan. With `--apply`, the runner:

1. Checks the active account, the project and billing.
2. Prepares resources and service accounts from `gcp/manage.py` and `gcp/notifications.py`.
3. Adds the Firestore index listed in `gcp/firestore.indexes.json` (the outbox scan) if it is missing, without
   deleting other indexes, then waits for READY. (The script's message still says "three": two indexes of the
   removed subscriber inbox were dropped from that file.)
4. Builds an image with a unique time tag, deploys the API and worker, and installs the recovery scheduler.
5. Checks that both services are Ready with every test-mode flag, API health 200, operator access without a
   Firebase token 401, and anonymous worker access denied.

The backend stays private at the IAM layer until the Hosting stage. Checks use a short-lived gcloud token held
in memory, never a JSON key or a printed token. The deploying account needs permission to invoke the services.
Resources already created are kept if a later step fails; there is no automatic destructive rollback.
After fixing the problem, the runner can be run again. Use `--version UNIQUE_TAG` to choose the tag.

**Rerunning this stage on a live project** needs the Owner role (IAM steps), makes the API private again
(`--no-allow-unauthenticated`) so the `hosting` stage must be rerun, and resets the mode to `dry_run`.
To only update the code, use the method below.

### Image-only update (Editor role is enough)

Build from `main`, then swap the image on both services. Env vars, service accounts, IAM, public API access,
the private worker and the schedule stay unchanged.

```bash
P=hack2skill-510921; R=asia-southeast2; TAG="fix-$(date -u +%Y%m%d-%H%M%S)"
IMG="$R-docker.pkg.dev/$P/asapify/api:$TAG"
gcloud builds submit . --project=$P --region=$R --tag="$IMG" \
  --service-account="projects/$P/serviceAccounts/asapify-build@$P.iam.gserviceaccount.com" \
  --gcs-source-staging-dir="gs://$P-asapify-builds/source" --gcs-log-dir="gs://$P-asapify-builds/logs"
for s in asapify-api asapify-notify; do
  gcloud run services update $s --project=$P --region=$R --image="$IMG" --update-env-vars="ASAPIFY_VERSION=$TAG"
done
curl -s https://hack2skill-510921.web.app/api/v1/health   # version must equal $TAG
```

Rollback: run the same `gcloud run services update` with the previous image tag.

### Automatic deployment (GitHub Actions)

`.github/workflows/deploy-backend.yml` runs the image-only update above whenever `main` changes in
`asapify_api/`, `asapify_engine/`, `asapify_agent/`, `main.py`, `Dockerfile`, `requirements-api.txt` or the
workflow itself (it can also be started manually with `workflow_dispatch`). Order: tests → build and push image `gh-<sha7>` → swap the image on both services →
smoke test through Hosting (health version = new tag, operator endpoint without a token = 401) → on failure,
automatic return to the previous image. Env vars, `dry_run`/`live` mode, IAM and the schedule are untouched.

Before the first run, an Owner runs this once from Cloud Shell (print the plan first):

```bash
bash gcp/github-deployer.sh
APPLY=1 bash gcp/github-deployer.sh
```

The script creates a keyless `asapify-be-deployer` account, adds the BE repository to the GitHub OIDC provider
the FE already uses, and gives that account push rights on the `asapify` image repository plus update rights on
`asapify-api` and `asapify-notify` only. Because `main` goes live directly, enable branch protection:
changes through PRs with passing tests. Also set an image retention policy in Artifact Registry, because every
deploy adds an image.

## 3. Set up Firebase and deploy Hosting

Add Firebase to the **same project**, register a Web App, enable the Google provider and add
`hack2skill-510921.web.app` to Authorized domains. If terms of service need accepting, the account owner does it
in the console. Use Node.js >=22.12 and the official Firebase CLI:

```bash
npm install -g firebase-tools
firebase login --no-localhost
```

If the CLI is already authenticated or uses ADC, signing in again is not needed. Follow the browser sign-in;
never share authentication values. Fill `ktp-asapify-fe/.env.production.local` from that Web App:

```dotenv
VITE_API_BASE=
VITE_FIREBASE_API_KEY=COPY_FROM_WEB_APP
VITE_FIREBASE_AUTH_DOMAIN=hack2skill-510921.firebaseapp.com
VITE_FIREBASE_PROJECT_ID=hack2skill-510921
VITE_FIREBASE_APP_ID=COPY_FROM_WEB_APP
```

Use the `authDomain` the Firebase config actually gives if it differs. The web configuration is a public identifier, not an Admin credential.
Keep this local configuration out of git. See also `docs/GCP.md` in the frontend repository.

From the backend repository root:

```bash
python3 -m gcp.cloudshell hosting --frontend ../ktp-asapify-fe
python3 -m gcp.cloudshell hosting --frontend ../ktp-asapify-fe --apply
```

This stage checks the configuration, builds the frontend in live mode, checks the backend, installs deny-all
rules on the `asapify` database only, gives `allUsers` **run.invoker on the API only**, then deploys Hosting.
The application still checks Firebase ID tokens and roles; the worker stays private. If the organisation forbids
public invokers, stop and ask an admin to choose an access design; the runner never changes organisation policy.

Do not use this stage on an `asapify` database that another application uses with different rules. Hosting
replaces the project's default site content with ASAPify; make sure that is the intended site. Review the plan first.

On success, the runner checks HTTP for the home page, `/join`, health through the rewrite, and refusal of
anonymous operator access. Expected URL after success: `https://hack2skill-510921.web.app/join`.
Do not call it live before the deployment and those checks pass.

## 4. Verify accounts and data

Open `/join` without signing in; it lists the team's alert group and every Telegram group that has an invite link. An admin
must check the UID/email in Firebase Authentication before using `tools/set_operator.py` to grant the
operator role. The runner never grants a role to any user account automatically.

To repeat only the backend checks:

```bash
python3 -m gcp.cloudshell verify --apply
```

HTTP checks do not prove that sign-in, Firestore transactions, provider delivery or the satellite pipeline work.
Until the first geo tick writes a slot, `DATA_NOT_READY` is the expected state. Read `docs/NOTIFICATIONS.md`
before enabling real channels. Record the URL and the first error message for diagnosis;
never copy tokens or logs that contain secrets.

References: [Cloud Run + Firebase Hosting](https://firebase.google.com/docs/hosting/cloud-run),
[Firebase CLI](https://firebase.google.com/docs/cli),
[Firestore composite indexes](https://docs.cloud.google.com/sdk/gcloud/reference/firestore/indexes/composite/create),
[Cloud Run developer authentication](https://docs.cloud.google.com/run/docs/authenticating/developers).
