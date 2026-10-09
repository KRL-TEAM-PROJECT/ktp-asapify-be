from gcp.manage import Deploy


def test_default_plan_never_launches_gcloud(monkeypatch, capsys):
    import subprocess
    def forbidden(*a, **kw):
        raise AssertionError("dry-run attempted execution")
    monkeypatch.setattr(subprocess, "run", forbidden)
    d = Deploy("asapify-test-project", "asia-southeast2", False)
    d.bootstrap()
    d.deploy("tested")
    plan = capsys.readouterr().out
    assert "--no-allow-unauthenticated" in plan
    assert "--public-access-prevention" in plan
    assert "--min=0" in plan and "--max=2" in plan
    assert "allUsers" not in plan
    assert "roles/owner" not in plan and "roles/editor" not in plan
