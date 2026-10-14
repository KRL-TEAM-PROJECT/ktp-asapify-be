"""Cloud Shell deployment runner. Prints a plan unless --apply is supplied.

Run from the backend checkout: python3 -m gcp.cloudshell backend --apply
See docs/CLOUDSHELL.md for authentication and the separate Hosting step.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from gcp.notifications import NotificationsDeploy

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "hack2skill-510921"
REGION = "asia-southeast2"
ORIGIN = f"https://{PROJECT}.web.app"


def command(args, *, apply, cwd=ROOT, env=None):
    print(f"[{cwd.name}] {shlex.join(args)}", flush=True)
    if apply:
        subprocess.run(args, cwd=cwd, env=env, check=True)


def index_key(index):
    group = index.get("collectionGroup")
    if not group:
        group = index["name"].split("/collectionGroups/")[1].split("/")[0]
    fields = tuple((f["fieldPath"], f.get("order"), f.get("arrayConfig"))
                   for f in index["fields"] if f["fieldPath"] != "__name__")
    return group, index.get("queryScope", "COLLECTION"), fields


def ensure_indexes(deploy):
    wanted = json.loads((ROOT / "gcp/firestore.indexes.json").read_text())["indexes"]
    current = deploy.run("firestore", "indexes", "composite", "list", "--database=asapify", read=True)
    existing = {index_key(index): index for index in current or []}
    for index in wanted:
        key = index_key(index)
        if key in existing:
            if existing[key].get("state") == "NEEDS_REPAIR":
                raise RuntimeError(f"Index {index['collectionGroup']} needs repair; no index was deleted.")
            continue
        fields = []
        for field in index["fields"]:
            config = f"field-path={field['fieldPath']}"
            if "order" in field:
                config += f",order={field['order'].lower()}"
            else:
                config += f",array-config={field['arrayConfig'].lower()}"
            fields.append(f"--field-config={config}")
        deploy.run("firestore", "indexes", "composite", "create", "--database=asapify",
                   f"--collection-group={index['collectionGroup']}", "--query-scope=collection",
                   *fields, "--async")
    if not deploy.apply:
        print("[verify] Wait for all three required indexes to reach READY; preserve other indexes.")
        return
    deadline = time.monotonic() + 600
    while True:
        current = deploy.run("firestore", "indexes", "composite", "list", "--database=asapify", read=True)
        existing = {index_key(index): index for index in current}
        states = [existing.get(index_key(index), {}).get("state", "MISSING") for index in wanted]
        if all(state == "READY" for state in states):
            return
        if "NEEDS_REPAIR" in states or time.monotonic() >= deadline:
            raise RuntimeError(f"Required indexes not ready: {states}. Rerun after resolving index status.")
        print(f"Waiting for Firestore indexes: {states}", flush=True)
        time.sleep(10)


def request(url, headers=None):
    try:
        with urlopen(Request(url, headers=headers or {}), timeout=60) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def verify_backend(deploy):
    services = {}
    for name in ("asapify-api", "asapify-notify"):
        service = deploy.run("run", "services", "describe", name, f"--region={REGION}", read=True)
        if not deploy.apply:
            continue
        if not any(c["type"] == "Ready" and c["status"] == "True" for c in service["status"]["conditions"]):
            raise RuntimeError(f"{name} is not Ready.")
        settings = {e["name"]: e.get("value") for e in service["spec"]["template"]["spec"]["containers"][0].get("env", [])}
        for key, value in {"NOTIFICATION_MODE": "dry_run", "FCM_ENABLED": "false", "TELEGRAM_ENABLED": "false"}.items():
            if settings.get(key) != value:
                raise RuntimeError(f"{name}: expected {key}={value}.")
        services[name] = service
    if not deploy.apply:
        print("[verify] Check API health=200, operator without Firebase token=401, anonymous worker denied.")
        return
    # Short-lived token stays in memory; never print it, put it in argv, or write it to a report.
    token = subprocess.run(["gcloud", "auth", "print-identity-token", f"--project={PROJECT}"],
                           check=True, capture_output=True, text=True).stdout.strip()
    headers = {"X-Serverless-Authorization": "Bearer " + token}
    api_url = services["asapify-api"]["status"]["url"]
    status, body = request(api_url + "/api/v1/health", headers)
    if status != 200 or json.loads(body).get("status") != "ok":
        raise RuntimeError(f"API health check failed: HTTP {status}.")
    status, _ = request(api_url + "/api/v1/operator/regions", headers)
    if status != 401:
        raise RuntimeError(f"Operator without Firebase token must return 401; received {status}.")
    worker_url = services["asapify-notify"]["status"]["url"]
    status, _ = request(worker_url + "/api/v1/health")
    if status not in (401, 403):
        raise RuntimeError(f"Anonymous worker access must be denied; received {status}.")
    print(f"Backend smoke checks passed. API: {api_url}; private worker: {worker_url}.")
    print("Real Firebase login, Firestore transactions, and end-to-end notifications still require integration tests.")


def hosting(deploy, frontend):
    if not frontend or not (frontend / "firebase.json").is_file():
        raise RuntimeError("Supply --frontend pointing to the frontend checkout with firebase.json.")
    if deploy.apply:
        for tool in ("node", "npm", "firebase"):
            if not shutil.which(tool):
                raise RuntimeError(f"Missing {tool}; see docs/CLOUDSHELL.md.")
        # All checks and the local build happen before API public exposure or Hosting changes.
        subprocess.run(["node", "-e", "const [a,b]=process.versions.node.split('.').map(Number); if(a<22||(a===22&&b<12)) process.exit(1)"], check=True)
    build_env = {**os.environ, "VITE_API_MODE": "live", "VITE_API_BASE": ""}
    command(["npm", "ci"], apply=deploy.apply, cwd=frontend)
    check_config = """
import { loadEnv } from 'vite';
const config = { ...loadEnv('production', process.cwd(), 'VITE_'), ...process.env };
const fields = ['API_KEY', 'AUTH_DOMAIN', 'PROJECT_ID', 'APP_ID'];
if (fields.some(k => !config['VITE_FIREBASE_' + k] || /COPY_FROM|YOUR_|SAME_PROJECT_ID|PLACEHOLDER/.test(config['VITE_FIREBASE_' + k]))) {
  throw new Error('Complete the Firebase Web App config in .env.production.local first.');
}
if (config.VITE_FIREBASE_PROJECT_ID !== 'hack2skill-510921') throw new Error('Firebase project mismatch');
console.log('Firebase public config present; real Google sign-in must still be tested.');
"""
    command(["node", "--input-type=module", "-e", check_config], apply=deploy.apply, cwd=frontend, env=build_env)
    command(["npm", "run", "build"], apply=deploy.apply, cwd=frontend, env=build_env)
    verify_backend(deploy)
    command(["firebase", "apps:list", "WEB", f"--project={PROJECT}", "--non-interactive"], apply=deploy.apply)
    # Rules target only the named ASAPify database; index creation never deletes other indexes.
    command(["firebase", "deploy", "--only=firestore:rules", "--config=firebase.notifications.json",
             f"--project={PROJECT}", "--non-interactive"], apply=deploy.apply, cwd=ROOT / "gcp")
    deploy.expose_api()
    command(["firebase", "deploy", "--only=hosting", f"--project={PROJECT}", "--non-interactive"],
            apply=deploy.apply, cwd=frontend)
    if deploy.apply:
        for path, expected in (("/", 200), ("/notifications", 200), ("/api/v1/health", 200),
                               ("/api/v1/operator/regions", 401)):
            status, body = request(ORIGIN + path)
            if status != expected:
                raise RuntimeError(f"Hosting {path}: expected {expected}, received {status}.")
            if path == "/api/v1/health" and json.loads(body).get("status") != "ok":
                raise RuntimeError("Hosting API rewrite did not return API health JSON.")
        print(f"Hosting HTTP checks passed: {ORIGIN}/notifications")
        print("Next: test Google sign-in and verify the operator UID before granting the operator role.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("backend", "hosting", "verify"))
    parser.add_argument("--frontend", type=Path)
    parser.add_argument("--version", default="notification-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    frontend = args.frontend.resolve() if args.frontend else None
    os.chdir(ROOT)
    deploy = NotificationsDeploy(PROJECT, REGION, args.apply)
    print(f"Target: {PROJECT}, {REGION}; notifications=dry_run; apply={args.apply}", flush=True)
    if args.apply:
        accounts = deploy.run("auth", "list", "--filter=status:ACTIVE", read=True)
        if not accounts:
            raise RuntimeError("No active gcloud account. Authorize Cloud Shell before running --apply.")
    if args.stage == "backend":
        deploy.bootstrap()
        deploy.bootstrap_notifications()
        ensure_indexes(deploy)
        deploy.build(args.version)
        deploy.deploy(args.version)
        deploy.deploy_worker(args.version, ORIGIN)
        verify_backend(deploy)
    elif args.stage == "hosting":
        deploy.preflight()
        hosting(deploy, frontend)
    else:
        deploy.preflight()
        verify_backend(deploy)
    if not args.apply:
        print("PLAN ONLY: no commands executed and no cloud resources changed. Add --apply to execute.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Deployment stopped: {error}. Review the failing step; any resources already created are retained.")
