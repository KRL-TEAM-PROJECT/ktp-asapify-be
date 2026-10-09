#!/usr/bin/env python3
"""Reviewable Google Cloud deployment. Default: print commands; --apply executes them.

Run in an authenticated Cloud Shell from the repository root. No service-account keys.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess

APIS = ["run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
        "firestore.googleapis.com", "storage.googleapis.com", "secretmanager.googleapis.com",
        "iam.googleapis.com", "identitytoolkit.googleapis.com", "firebase.googleapis.com"]


class Deploy:
    def __init__(self, project: str, region: str, apply: bool):
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
            raise ValueError("Isi Project ID GCP yang sah, bukan nama tampilan.")
        if region != "asia-southeast2":
            raise ValueError("Paket ini memakai Jakarta; ubah juga firebase.json bila mengganti region.")
        self.project, self.region, self.apply = project, region, apply
        self.data = f"gs://{project}-asapify-data"
        self.builds = f"gs://{project}-asapify-builds"
        self.api = f"asapify-api@{project}.iam.gserviceaccount.com"
        self.builder = f"asapify-build@{project}.iam.gserviceaccount.com"
        if apply and not shutil.which("gcloud"):
            raise RuntimeError("Jalankan dari Cloud Shell atau instal Google Cloud CLI resmi.")

    def run(self, *args: str, read=False):
        command = ["gcloud", *args, f"--project={self.project}", "--quiet"]
        if read:
            command.append("--format=json")
        print(shlex.join(command), flush=True)
        if not self.apply:
            return None
        result = subprocess.run(command, check=True, capture_output=read, text=True)
        return json.loads(result.stdout) if read else None

    def preflight(self):
        project = self.run("projects", "describe", self.project, read=True)
        billing = self.run("billing", "projects", "describe", self.project, read=True)
        if self.apply and (project.get("lifecycleState") != "ACTIVE" or not billing.get("billingEnabled")):
            raise RuntimeError("Project harus aktif dan billing sudah ditautkan sebelum deployment.")

    def bootstrap(self):
        self.preflight()
        self.run("services", "enable", *APIS)
        accounts = self.run("iam", "service-accounts", "list", read=True)
        known = {a["email"] for a in accounts or []}
        for name, email in [("asapify-api", self.api), ("asapify-build", self.builder)]:
            if email not in known:
                self.run("iam", "service-accounts", "create", name, f"--display-name={name}")
        buckets = self.run("storage", "buckets", "list", read=True)
        known_buckets = {b["name"].removeprefix("gs://").rstrip("/") for b in buckets or []}
        for bucket in [self.data, self.builds]:
            if bucket.removeprefix("gs://") not in known_buckets:
                self.run("storage", "buckets", "create", bucket, f"--location={self.region}",
                         "--uniform-bucket-level-access", "--public-access-prevention")
        databases = self.run("firestore", "databases", "list", read=True)
        database = next((d for d in databases or [] if d["name"].endswith("/asapify")), None)
        if database:
            if database.get("locationId") != self.region or database.get("type") != "FIRESTORE_NATIVE":
                raise RuntimeError("Database asapify sudah ada dengan lokasi/tipe berbeda; tinjau sebelum lanjut.")
        else:
            self.run("firestore", "databases", "create", "--database=asapify", f"--location={self.region}",
                     "--type=firestore-native", "--delete-protection")
        repos = self.run("artifacts", "repositories", "list", f"--location={self.region}", read=True)
        if not any(r["name"].endswith("/asapify") for r in repos or []):
            self.run("artifacts", "repositories", "create", "asapify", f"--location={self.region}", "--repository-format=docker")
        # Dedicated project: datastore.user is project-wide; no Owner/Editor on any runtime account.
        for role in ["roles/datastore.user", "roles/firebaseauth.viewer"]:
            self.run("projects", "add-iam-policy-binding", self.project,
                     f"--member=serviceAccount:{self.api}", f"--role={role}", "--condition=None")
        self.run("storage", "buckets", "add-iam-policy-binding", self.data,
                 f"--member=serviceAccount:{self.api}", "--role=roles/storage.objectViewer")
        self.run("storage", "buckets", "add-iam-policy-binding", self.builds,
                 f"--member=serviceAccount:{self.builder}", "--role=roles/storage.objectAdmin")
        self.run("artifacts", "repositories", "add-iam-policy-binding", "asapify", f"--location={self.region}",
                 f"--member=serviceAccount:{self.builder}", "--role=roles/artifactregistry.writer")
        print("Bootstrap selesai hanya bila semua perintah berhasil. Firebase Auth diaktifkan terpisah; lihat docs/GCP.md.")

    def image(self, version: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", version):
            raise ValueError("Versi image tidak sah.")
        return f"{self.region}-docker.pkg.dev/{self.project}/asapify/api:{version}"

    def build(self, version: str):
        self.preflight()
        if not Path("Dockerfile").is_file():
            raise RuntimeError("Jalankan dari root repo backend yang berisi Dockerfile.")
        self.run("builds", "submit", ".", f"--tag={self.image(version)}", f"--region={self.region}",
                 f"--service-account=projects/{self.project}/serviceAccounts/{self.builder}",
                 f"--gcs-source-staging-dir={self.builds}/source", f"--gcs-log-dir={self.builds}/logs")

    def deploy(self, version: str):
        self.preflight()
        self.run("run", "deploy", "asapify-api", f"--image={self.image(version)}", f"--region={self.region}",
                 f"--service-account={self.api}", "--no-allow-unauthenticated", "--port=8080",
                 "--min=0", "--max=2", "--cpu=1", "--memory=512Mi", "--timeout=60", "--concurrency=40",
                 "--update-env-vars=" + ",".join([
                     f"GOOGLE_CLOUD_PROJECT={self.project}", "FIRESTORE_DATABASE=asapify",
                     f"ASAPIFY_DATA_BUCKET={self.data.removeprefix('gs://')}",
                     "TELEGRAM_ENABLED=false", f"ASAPIFY_VERSION={version}"]))
        self.run("run", "services", "describe", "asapify-api", f"--region={self.region}", read=True)

    def expose_api(self):
        # Firebase Hosting needs public transport; EVERY /operator endpoint still verifies Firebase tokens.
        self.run("run", "services", "add-iam-policy-binding", "asapify-api", f"--region={self.region}",
                 "--member=allUsers", "--role=roles/run.invoker")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["check", "bootstrap", "build", "deploy", "expose-api"])
    p.add_argument("--project", required=True)
    p.add_argument("--region", default="asia-southeast2")
    p.add_argument("--version", default="operator-v1")
    p.add_argument("--apply", action="store_true", help="Eksekusi perintah yang dicetak; tanpa ini hanya rencana.")
    args = p.parse_args()
    d = Deploy(args.project, args.region, args.apply)
    {"check": d.preflight, "bootstrap": d.bootstrap, "build": lambda: d.build(args.version),
     "deploy": lambda: d.deploy(args.version), "expose-api": d.expose_api}[args.action]()


if __name__ == "__main__":
    main()
