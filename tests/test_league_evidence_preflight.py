from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import plistlib
from datetime import date
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def _live_sports_fixture(tmp_path):
    """A loaded LaunchDaemon and manifest bound to actual test files."""
    sha = "a" * 40
    runtime = tmp_path / "runtime"
    (runtime / "run").mkdir(parents=True)
    release = runtime / "releases" / sha
    (release / "scripts").mkdir(parents=True)
    entrypoint = release / "scripts/parallax_unattended.py"
    entrypoint.write_text("print('supervisor')\n", encoding="utf-8")
    lock = release / "requirements.lock"
    lock.write_text("example==1.0\n", encoding="utf-8")
    runtime_env = runtime / ".env.runtime"
    runtime_env.write_text("PARALLAX_ALERT_MODE=ntfy\n", encoding="utf-8")
    plist_path = tmp_path / "com.swarmedge.parallax-sports.plist"
    plist_path.write_bytes(plistlib.dumps({
        "Label": "com.swarmedge.parallax-sports",
        "ProgramArguments": [
            str(release / ".venv/bin/python"), str(entrypoint),
            "--root", str(release), "--runtime-env", str(runtime_env),
        ],
        "EnvironmentVariables": {"PARALLAX_RELEASE_SHA": sha},
    }))
    def sha256(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    payload = {
        "schema_version": 1,
        "git_sha": sha,
        "dependency_lock_sha256": sha256(lock),
        "runtime_config_sha256": sha256(runtime_env),
        "launchd_plist_sha256": sha256(plist_path),
        "runtime_entrypoint_sha256": sha256(entrypoint),
    }
    manifest = runtime / "run/parallax-sports-manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    launchctl_output = (
        f"state = running\npath = {plist_path}\n"
        f"program = {entrypoint}\n"
    )
    return runtime, manifest, payload, launchctl_output, entrypoint, runtime_env, plist_path


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
    runtime, manifest, payload, output, _, _, _ = _live_sports_fixture(tmp_path)
    sha = payload["git_sha"]
    monkeypatch.setattr(preflight_module, "_launchctl", lambda target: (
        True, output if target.endswith("com.swarmedge.parallax-sports") else ""))
    result = preflight_module.preflight(runtime_root=runtime, authorization_file=auth_file,
                                        credential_file=credential)
    assert result["ready_for_isolated_nba_only"] is True
    assert result["manifest_integrity_verified"] is True
    assert result["sports_job_running"] is True
    assert result["active_sports_sha"] == result["manifest_sha"] == sha
    assert result["authorization"]["NBA"]["state"] == "AUTHORIZED"
    assert result["authorization"]["NHL"]["state"] == "UNVERIFIED"
    assert "credential-never-print" not in json.dumps(result)

    manifest.write_text(json.dumps({**payload, "git_sha": "b" * 40}), encoding="utf-8")
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
    runtime, manifest, payload, output, _, _, _ = _live_sports_fixture(tmp_path)

    def fake_launchctl(target):
        if target == "system/com.swarmedge.parallax-sports":
            return True, output
        return False, ""

    monkeypatch.setattr(preflight_module, "_launchctl", fake_launchctl)
    result = preflight_module.preflight(runtime_root=runtime,
        authorization_file=auth_file, credential_file=credential)
    assert result["ready_for_isolated_nba_only"] is True
    assert result["sports_job_domain"] == "system"
    assert result["manifest_integrity_verified"] is True
    assert result["gui_domain_available"] is False
    assert "user_gui_domain_unavailable" not in result["blockers"]

    manifest.write_text(json.dumps({**payload, "git_sha": "b" * 40}), encoding="utf-8")
    drift = preflight_module.preflight(runtime_root=runtime,
        authorization_file=auth_file, credential_file=credential)
    assert drift["ready_for_isolated_nba_only"] is False
    assert "active_release_manifest_sha_mismatch" in drift["blockers"]



def test_preflight_rejects_hash_drift_and_missing_manifest_fields(tmp_path, monkeypatch):
    module = _module("league_evidence_preflight_hashes", "league_evidence_preflight.py")
    runtime, manifest, payload, output, entrypoint, runtime_env, plist = (
        _live_sports_fixture(tmp_path)
    )
    monkeypatch.setattr(module, "_launchctl", lambda target: (
        True, output if target.endswith("com.swarmedge.parallax-sports") else ""
    ))
    auth = tmp_path / "absent-auth"
    key = tmp_path / "absent-key"

    def inspect():
        return module.preflight(runtime_root=runtime, authorization_file=auth,
                                credential_file=key)

    assert inspect()["manifest_integrity_verified"] is True
    for file in (entrypoint, runtime_env, plist):
        original = file.read_bytes()
        file.write_bytes(original + b"\n")
        drift = inspect()
        assert drift["manifest_integrity_verified"] is False
        assert "sports_manifest_content_hashes_unverified" in drift["blockers"]
        file.write_bytes(original)

    original_plist = plist.read_bytes()
    for mutation in ("bad_label", "bad_environment"):
        value = plistlib.loads(original_plist)
        if mutation == "bad_label":
            value["Label"] = "com.swarmedge.unrelated"
        else:
            value["EnvironmentVariables"] = "not-a-dictionary"
        plist.write_bytes(plistlib.dumps(value))
        assert inspect()["manifest_integrity_verified"] is False
        assert "sports_manifest_content_hashes_unverified" in inspect()["blockers"]
    plist.write_bytes(original_plist)

    manifest.write_text(json.dumps({k: v for k, v in payload.items()
                                    if k != "dependency_lock_sha256"}), encoding="utf-8")
    assert "sports_manifest_content_hashes_unverified" in inspect()["blockers"]


def test_preflight_rejects_non_running_or_unresolved_launchd_job(tmp_path, monkeypatch):
    module = _module("league_evidence_preflight_stopped", "league_evidence_preflight.py")
    runtime, _, _, output, _, _, _ = _live_sports_fixture(tmp_path)
    auth = tmp_path / "absent-auth"
    key = tmp_path / "absent-key"

    def inspect():
        return module.preflight(runtime_root=runtime, authorization_file=auth,
                                credential_file=key)

    monkeypatch.setattr(module, "_launchctl", lambda target: (
        True, output.replace("state = running", "state = waiting")
        if target.endswith("com.swarmedge.parallax-sports") else ""
    ))
    stopped = inspect()
    assert stopped["sports_job_running"] is False
    assert "production_sports_job_not_running" in stopped["blockers"]

    monkeypatch.setattr(module, "_launchctl", lambda target: (
        True, output.replace("path = ", "unknown = ")
        if target.endswith("com.swarmedge.parallax-sports") else ""
    ))
    unresolved = inspect()
    assert unresolved["manifest_integrity_verified"] is False
    assert "sports_manifest_content_hashes_unverified" in unresolved["blockers"]


def test_runtime_diagnostic_resolves_system_launchdaemon(monkeypatch):
    """Diagnostic must inspect system launchd before declaring production absent."""
    from types import SimpleNamespace
    diagnostic = _module("diagnose_league_runtime_system", "diagnose_league_runtime.py")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args == ["launchctl", "print", "system/com.swarmedge.parallax-sports"]:
            return SimpleNamespace(returncode=0,
                stdout="state = running\npid = 12345\n", stderr="")
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
            return SimpleNamespace(returncode=0, stdout="state = running\n", stderr="")
        return SimpleNamespace(returncode=113, stdout="", stderr="not found")

    monkeypatch.setattr(diagnostic.subprocess, "run", fake_run)
    result = diagnostic._launchd_job("com.swarmedge.parallax-sports")
    assert result["available"] is True
    assert result["domain"] == f"gui/{uid}"


def test_runtime_diagnostic_extracts_sha_from_nested_venv_path(tmp_path, monkeypatch, capsys):
    """Nested venv interpreter must resolve to the release SHA, not '.venv'."""
    import plistlib
    from types import SimpleNamespace
    diagnostic = _module("diagnose_league_runtime_release", "diagnose_league_runtime.py")
    sha = "a" * 40
    runtime = tmp_path / "runtime"
    (runtime / "run").mkdir(parents=True)
    (runtime / "state/parallax").mkdir(parents=True)
    (runtime / "run/parallax-sports-manifest.json").write_text(
        json.dumps({"git_sha": sha}), encoding="utf-8")
    (runtime / "state/parallax/parallax_unattended_health.json").write_text(
        json.dumps({"release_sha": sha}), encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    plist = staging / "com.swarmedge.parallax-sports.test.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": "com.swarmedge.parallax-sports",
        "ProgramArguments": [
            f"/opt/swarm/releases/{sha}/.venv/bin/python",
            f"/opt/swarm/releases/{sha}/scripts/parallax_unattended.py",
            "--state-dir", str(runtime / "state/parallax"),
        ],
    }))
    monkeypatch.setattr(diagnostic, "_launchd_job", lambda label:
        {"available": True, "domain": "system", "summary": ["state = running"], "error": None})
    args = SimpleNamespace(runtime_root=str(runtime), manifest=None, health=None,
        plist_dir=str(staging), plist_pattern="*.plist")
    assert diagnostic.diagnose(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["plists"][0]["release_sha_from_argument"] == sha
    assert "manifest_git_sha_differs_from_plist_release" not in report["mismatches"]
