"""Deployment safeguards; no cloud access or provider calls."""
from contextlib import redirect_stdout
import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gcp.cloudshell import ROOT, ensure_indexes, verify_backend
from gcp.notifications import NotificationsDeploy


def test_plan_never_executes_cloud_commands():
    deployment = NotificationsDeploy("hack2skill-510921", "asia-southeast2", False)
    with patch("subprocess.run", side_effect=AssertionError("Plan executed a command")):
        deployment.bootstrap()
        deployment.bootstrap_notifications()
        ensure_indexes(deployment)
        deployment.build("plan-test")
        deployment.deploy("plan-test")
        deployment.deploy_worker("plan-test", "https://hack2skill-510921.web.app")
        verify_backend(deployment)


def test_deployment_resets_notification_modes_on_every_revision():
    output = io.StringIO()
    deployment = NotificationsDeploy("hack2skill-510921", "asia-southeast2", False)
    with redirect_stdout(output):
        deployment.deploy("mode-test")
        deployment.deploy_worker("mode-test", "https://hack2skill-510921.web.app")
    changes = [line for line in output.getvalue().splitlines()
               if line.startswith("gcloud run ") and "--update-env-vars=" in line]
    assert len(changes) == 4
    for line in changes:
        for setting in ("NOTIFICATION_MODE=dry_run", "FCM_ENABLED=false", "TELEGRAM_ENABLED=false"):
            assert setting in line
    assert all("--no-allow-unauthenticated" in line for line in changes if "run deploy" in line)


def test_existing_indexes_are_preserved_with_server_generated_name_field():
    wanted = json.loads((ROOT / "gcp/firestore.indexes.json").read_text())["indexes"]
    current = []
    for i, spec in enumerate(wanted):
        index = copy.deepcopy(spec)
        group = index.pop("collectionGroup")
        index.update(name=f"projects/p/databases/asapify/collectionGroups/{group}/indexes/{i}", state="READY")
        index["fields"].append({"fieldPath": "__name__", "order": "ASCENDING"})
        current.append(index)
    unrelated = copy.deepcopy(current[0])
    unrelated["name"] = unrelated["name"].replace("notification_inbox", "unrelated_application")
    current.append(unrelated)
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        assert args[:4] == ("firestore", "indexes", "composite", "list")
        return current

    ensure_indexes(SimpleNamespace(apply=True, run=run))
    assert len(calls) == 2


@pytest.mark.parametrize("worker_status,expected_error", [(200, True), (403, False)])
def test_smoke_check_rejects_public_worker_and_never_prints_token(worker_status, expected_error, capsys):
    service = {"status": {"conditions": [{"type": "Ready", "status": "True"}], "url": "https://test.run.app"},
               "spec": {"template": {"spec": {"containers": [{"env": [
                   {"name": "NOTIFICATION_MODE", "value": "dry_run"},
                   {"name": "FCM_ENABLED", "value": "false"},
                   {"name": "TELEGRAM_ENABLED", "value": "false"}]}]}}}}
    deployment = SimpleNamespace(apply=True, run=lambda *a, **kw: service)
    responses = [(200, b'{"status":"ok"}'), (401, b""), (worker_status, b"")]
    with patch("subprocess.run", return_value=SimpleNamespace(stdout="test-secret-token\n")), \
         patch("gcp.cloudshell.request", side_effect=responses):
        if expected_error:
            with pytest.raises(RuntimeError, match="Anonymous worker"):
                verify_backend(deployment)
        else:
            verify_backend(deployment)
    assert "test-secret-token" not in capsys.readouterr().out
