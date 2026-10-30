#!/usr/bin/env python3
"""Read-only Mac user-session preflight for isolated NBA evidence collection."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect_league_evidence import authorization_state


def _launchctl(target: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(["launchctl", "print", target], capture_output=True,
            text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, "unavailable"
    return result.returncode == 0, result.stdout if result.returncode == 0 else ""


def _credential_ready(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and 1 <= info.st_size <= 512)
    finally:
        os.close(fd)


def _sha256(path: Path) -> str | None:
    """Hash local release inputs without reading secrets into a report."""
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _manifest_matches_running_release(
    runtime_root: Path, job_output: str, active_sha: str | None, manifest: dict
) -> bool:
    """Verify the loaded plist and all four pinned release inputs, fail closed.

    launchctl's 'path' identifies the loaded job definition, not a potentially
    stale candidate plist in staging. Never infer integrity from Git SHA alone.
    """
    if (
        not active_sha
        or not re.fullmatch(r"[0-9a-f]{40}", active_sha)
        or not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("git_sha") != active_sha
    ):
        return False
    loaded = re.search(r"(?m)^\s*path\s*=\s*(/.*?\.plist)\s*$", job_output)
    if not loaded:
        return False
    plist_path = Path(loaded.group(1))
    try:
        plist = plistlib.loads(plist_path.read_bytes())
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(plist, dict) or plist.get("Label") != "com.swarmedge.parallax-sports":
        return False
    args = plist.get("ProgramArguments")
    env = plist.get("EnvironmentVariables")
    if (
        not isinstance(args, list)
        or not all(isinstance(arg, str) for arg in args)
        or not isinstance(env, dict)
    ):
        return False
    release = (runtime_root / "releases" / active_sha).resolve()
    entrypoint = release / "scripts/parallax_unattended.py"
    if (
        args.count("--root") != 1
        or args.count("--runtime-env") != 1
        or args.index("--root") + 1 >= len(args)
        or args.index("--runtime-env") + 1 >= len(args)
        or Path(args[args.index("--root") + 1]).resolve() != release
        or not any(
            arg.endswith("/scripts/parallax_unattended.py")
            and Path(arg).resolve() == entrypoint
            for arg in args
        )
        or env.get("PARALLAX_RELEASE_SHA") != active_sha
    ):
        return False
    checks = {
        "dependency_lock_sha256": release / "requirements.lock",
        "runtime_entrypoint_sha256": entrypoint,
        "runtime_config_sha256": Path(args[args.index("--runtime-env") + 1]),
        "launchd_plist_sha256": plist_path,
    }
    return all(
        isinstance(manifest.get(field), str)
        and re.fullmatch(r"[0-9a-f]{64}", manifest[field])
        and _sha256(path) == manifest[field]
        for field, path in checks.items()
    )


def preflight(*, runtime_root: Path, authorization_file: Path,
              credential_file: Path) -> dict:
    uid = os.getuid()
    domain_ok, _ = _launchctl(f"gui/{uid}")
    # The Mac mini production sports controller is a system LaunchDaemon.
    # Do not require a GUI session when the system job is available.
    job_domain = None
    job_output = ""
    for domain in ("system", f"gui/{uid}", f"user/{uid}"):
        available, output = _launchctl(f"{domain}/com.swarmedge.parallax-sports")
        if available:
            job_domain = domain
            job_output = output
            break
    job_ok = job_domain is not None
    job_running = bool(re.search(r"(?m)^\s*state\s*=\s*running\s*$", job_output))
    active = re.search(r"/releases/([0-9a-f]{40})(?:/|\b)", job_output)
    active_sha = active.group(1) if active else None
    # The deployment contract writes this manifest inside each immutable
    # release. Legacy runtime manifests are supported only when the
    # documented release manifest does not exist, never as a fallback
    # from a present but invalid release manifest.
    if active_sha and re.fullmatch(r"[0-9a-f]{40}", active_sha):
        documented_manifest = (
            runtime_root / "releases" / active_sha / "deploy/manifest.json"
        )
    else:
        documented_manifest = None
    manifest_path = (
        documented_manifest
        if documented_manifest is not None and documented_manifest.exists()
        else runtime_root / "run/parallax-sports-manifest.json"
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_sha = manifest.get("git_sha") if isinstance(manifest, dict) else None
    except (OSError, json.JSONDecodeError):
        manifest = {}
        manifest_sha = None
    if not isinstance(manifest_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", manifest_sha):
        manifest_sha = None
    drift = bool(active_sha and manifest_sha and active_sha != manifest_sha)
    manifest_integrity_ok = _manifest_matches_running_release(
        runtime_root, job_output, active_sha, manifest
    )
    try:
        authorization = json.loads(authorization_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        authorization = {}
    nba_ok, nba_auth = authorization_state("NBA", authorization)
    nhl_ok, nhl_auth = authorization_state("NHL", authorization)
    credential_ok = _credential_ready(credential_file)
    blockers = []
    if not domain_ok and job_domain != "system":
        blockers.append("user_gui_domain_unavailable")
    if not job_ok:
        blockers.append("production_sports_job_unavailable")
    elif not job_running:
        blockers.append("production_sports_job_not_running")
    if not active_sha:
        blockers.append("active_sports_release_sha_unresolved")
    if not manifest_sha:
        blockers.append("sports_manifest_sha_unavailable")
    if drift:
        blockers.append("active_release_manifest_sha_mismatch")
    if not manifest_integrity_ok:
        blockers.append("sports_manifest_content_hashes_unverified")
    if not credential_ok:
        blockers.append("nba_api_credential_missing_or_unsafe")
    if not nba_ok:
        blockers.append("nba_account_plan_or_terms_unverified")
    return {
        "read_only": True,
        "ready_for_isolated_nba_only": not blockers,
        "gui_domain_available": domain_ok,
        "sports_job_loaded": job_ok,
        "sports_job_running": job_running,
        "manifest_integrity_verified": manifest_integrity_ok,
        "sports_job_domain": job_domain,
        "active_sports_sha": active_sha,
        "manifest_sha": manifest_sha,
        "manifest_drift": drift,
        "nba_credential_ready": credential_ok,
        "authorization": {"NBA": nba_auth, "NHL": nhl_auth},
        "nhl_authorized": nhl_ok,
        "blockers": blockers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path,
        default=Path.home() / "Library/Application Support/SwarmEdge")
    parser.add_argument("--authorization-file", type=Path,
        default=Path.home() / ".config/parallax/league-evidence-authorization.json")
    parser.add_argument("--credential-file", type=Path,
        default=Path.home() / "parallax-research/nba/.bdl-api-key")
    args = parser.parse_args(argv)
    result = preflight(runtime_root=args.runtime_root,
        authorization_file=args.authorization_file, credential_file=args.credential_file)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ready_for_isolated_nba_only"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
