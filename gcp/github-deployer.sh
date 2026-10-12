#!/usr/bin/env bash
# One-time setup by a project Owner so .github/workflows/deploy-backend.yml can deploy.
# Prints the commands by default; run with APPLY=1 to execute them (Cloud Shell, project Owner).
#
# Grants, all scoped as narrowly as gcloud allows, with no service-account key:
#   - the existing GitHub OIDC provider also accepts KRL-TEAM-PROJECT/ktp-asapify-be (FE keeps working);
#   - only that repository may act as asapify-be-deployer;
#   - asapify-be-deployer may push to the `asapify` image repository and update the two Cloud Run
#     services, running as their existing runtime accounts. No Firestore, Storage or project-wide role.
set -euo pipefail

P=hack2skill-510921
N=950889990251
R=asia-southeast2
SA="asapify-be-deployer@$P.iam.gserviceaccount.com"
REPO=KRL-TEAM-PROJECT/ktp-asapify-be
POOL="projects/$N/locations/global/workloadIdentityPools/github-actions"

run() {
  echo "+ $*"
  if [ "${APPLY:-0}" = 1 ]; then "$@"; fi
}

if ! gcloud iam service-accounts describe "$SA" --project="$P" >/dev/null 2>&1; then
  run gcloud iam service-accounts create asapify-be-deployer --project="$P" \
    --display-name="GitHub Actions deploy for ktp-asapify-be"
fi

run gcloud iam workload-identity-pools providers update-oidc github \
  --workload-identity-pool=github-actions --location=global --project="$P" \
  --attribute-condition="assertion.repository_owner == 'KRL-TEAM-PROJECT' && assertion.repository in ['KRL-TEAM-PROJECT/ktp-asapify-fe', 'KRL-TEAM-PROJECT/ktp-asapify-be']"

run gcloud iam service-accounts add-iam-policy-binding "$SA" --project="$P" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/$POOL/attribute.repository/$REPO"

run gcloud artifacts repositories add-iam-policy-binding asapify --location="$R" --project="$P" \
  --role=roles/artifactregistry.writer --member="serviceAccount:$SA"

for s in asapify-api asapify-notify; do
  run gcloud run services add-iam-policy-binding "$s" --region="$R" --project="$P" \
    --role=roles/run.developer --member="serviceAccount:$SA"
  run gcloud iam service-accounts add-iam-policy-binding "$s@$P.iam.gserviceaccount.com" --project="$P" \
    --role=roles/iam.serviceAccountUser --member="serviceAccount:$SA"
done

if [ "${APPLY:-0}" != 1 ]; then
  echo "Plan only. Review, then run: APPLY=1 bash gcp/github-deployer.sh"
fi
