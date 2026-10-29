from __future__ import annotations

import importlib.util
import json
import os
from datetime import date
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_read_only_preflight_detects_host_readiness_and_manifest_drift(tmp_path, monkeypatch):
    preflight_module = _module("league_evidence_preflight", "league_evidence_preflight.py")
    today = date.today().isoformat()
    auth = {"NBA": {"provider": "BALLDONTLIE", "api_plan": "verified-plan",
        "nba_games_access": True, "account_plan_verified": True,
        "account_plan_verified_at": today, "terms_reviewed": True,
        "terms_reviewed_at": today,
        "terms_url": "https://www.balldontlie.io/terms.html"}}
    auth_file = tmp_path / "authorization.json"
    auth_file.write_text(json.dumps(auth), encoding="utf-8")
    credential = tmp_path / ".bdl-api-key"
    credential.write_text("credential-never-print", encoding="ascii")
    credential.chmod(0o600)
    runtime = tmp_path / "runtime"
    (runtime / "run").mkdir(parents=True)
    sha = "a" * 40
    manifest = runtime / "run/parallax-sports-manifest.json"
    manifest.write_text(json.dumps({"git_sha": sha}), encoding="utf-8")
    monkeypatch.setattr(preflight_module, "_launchctl", lambda target: (
        True, "path = /opt/swarm/releases/" + sha + "/scripts/parallax_unattended.py"
        if target.endswith("com.swarmedge.parallax-sports") else ""))
    result = preflight_module.preflight(runtime_root=runtime, authorization_file=auth_file,
                                        credential_file=credential)
    assert result["ready_for_isolated_nba_only"] is True
    assert result["active_sports_sha"] == result["manifest_sha"] == sha
    assert result["authorization"]["NBA"]["state"] == "AUTHORIZED"
    assert result["authorization"]["NHL"]["state"] == "UNVERIFIED"
    assert "credential-never-print" not in json.dumps(result)

    manifest.write_text(json.dumps({"git_sha": "b" * 40}), encoding="utf-8")
    drift = preflight_module.preflight(runtime_root=runtime, authorization_file=auth_file,
                                       credential_file=credential)
    assert drift["manifest_drift"] is True
    assert drift["ready_for_isolated_nba_only"] is False
    assert "active_release_manifest_sha_mismatch" in drift["blockers"]


def test_read_only_preflight_rejects_group_readable_api_key(tmp_path, monkeypatch):
    preflight_module = _module("league_evidence_preflight", "league_evidence_preflight.py")
    monkeypatch.setattr(preflight_module, "_launchctl", lambda _target: (False, ""))
    credential = tmp_path / ".bdl-api-key"
    credential.write_text("private", encoding="ascii")
    credential.chmod(0o644)
    result = preflight_module.preflight(runtime_root=tmp_path / "missing",
        authorization_file=tmp_path / "missing-auth.json", credential_file=credential)
    assert result["nba_credential_ready"] is False
    assert "nba_api_credential_missing_or_unsafe" in result["blockers"]


def test_system_launchdaemon_is_valid_without_gui_session(tmp_path, monkeypatch):
    """A live system-domain sports job must not be rejected as GUI unavailable."""
    preflight_module = _module("league_evidence_preflight_system", "league_evidence_preflight.py")
    today = date.today().isoformat()
    auth = {"NBA": {"provider": "BALLDONTLIE", "api_plan": "verified-plan",
        "nba_games_access": True, "account_plan_verified": True,
        "account_plan_verified_at": today, "terms_reviewed": True,
        "terms_reviewed_at": today,
        "terms_url": "https://www.balldontlie.io/terms.html"}}
    auth_file = tmp_path / "authorization.json"
    auth_file.write_text(json.dumps(auth), encoding="utf-8")
    credential = tmp_path / ".bdl-api-key"
    credential.write_text("test-secret", encoding="ascii")
    credential.chmod(0o600)
    runtime = tmp_path / "runtime"
    (runtime / "run").mkdir(parents=True)
    sha = "a" * 40
    (runtime / "run/parallax-sports-manifest.json").write_text(
        json.dumps({"git_sha": sha}), encoding="utf-8")

    def fake_launchctl(target):
        if target == "system/com.swarmedge.parallax-sports":
            return True, "state = running\\npath = /opt/swarm/releases/" + sha + "/scripts/parallax_unattended.py"
        return False, ""

    monkeypatch.setattr(preflight_module, "_launchctl", fake_launchctl)
    result = preflight_module.preflight(runtime_root=runtime,
        authorization_file=auth_file, credential_file=credential)
    assert result["ready_for_isolated_nba_only"] is True
    assert result["sports_job_domain"] == "system"
    assert result["gui_domain_available"] is False
    assert "user_gui_domain_unavailable" not in result["blockers"]

    (runtime / "run/parallax-sports-manifest.json").write_text(
        json.dumps({"git_sha": "b" * 40}), encoding="utf-8")
    drift = preflight_module.preflight(runtime_root=runtime,
        authorization_file=auth_file, credential_file=credential)
    assert drift["ready_for_isolated_nba_only"] is False
    assert "active_release_manifest_sha_mismatch" in drift["blockers"]


def test_runtime_diagnostic_resolves_system_launchdaemon(monkeypatch):
    """Diagnostic must inspect system launchd before declaring production absent."""
    from types import SimpleNamespace
    diagnostic = _module("diagnose_league_runtime_system", "diagnose_league_runtime.py")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args == ["launchctl", "print", "system/com.swarmedge.parallax-sports"]:
            return SimpleNamespace(returncode=0,
                stdout="state = running\\npid = 12345\\n", stderr="")
        return SimpleNamespace(returncode=113, stdout="", stderr="not found")

    monkeypatch.setattr(diagnostic.subprocess, "run", fake_run)
    result = diagnostic._launchd_job("com.swarmedge.parallax-sports")
    assert result["available"] is True
    assert result["domain"] == "system"
    assert "state = running" in result["summary"]
    assert calls == [["launchctl", "print", "system/com.swarmedge.parallax-sports"]]


def test_runtime_diagnostic_falls_back_to_gui_domain(monkeypatch):
    from types import SimpleNamespace
    diagnostic = _module("diagnose_league_runtime_gui", "diagnose_league_runtime.py")
    uid = os.getuid()

    def fake_run(args, **kwargs):
        if args[-1] == f"gui/{uid}/com.swarmedge.parallax-sports":
            return SimpleNamespace(returncode=0, stdout="state = running\\n", stderr="")
        return SimpleNamespace(returncode=113, stdout="", stderr="not found")

    monkeypatch.setattr(diagnostic.subprocess, "run", fake_run)
    result = diagnostic._launchd_job("com.swarmedge.parallax-sports")
    assert result["available"] is True
    assert result["domain"] == f"gui/{uid}"
