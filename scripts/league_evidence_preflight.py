#!/usr/bin/env python3
"""Read-only Mac user-session preflight for isolated NBA evidence collection."""
from __future__ import annotations

import argparse
import json
import os
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
    active = re.search(r"/releases/([0-9a-f]{40})(?:/|\b)", job_output)
    active_sha = active.group(1) if active else None
    manifest_path = runtime_root / "run/parallax-sports-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_sha = manifest.get("git_sha")
    except (OSError, json.JSONDecodeError):
        manifest_sha = None
    drift = bool(active_sha and manifest_sha and active_sha != manifest_sha)
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
    if not active_sha:
        blockers.append("active_sports_release_sha_unresolved")
    if not manifest_sha:
        blockers.append("sports_manifest_sha_unavailable")
    if drift:
        blockers.append("active_release_manifest_sha_mismatch")
    if not credential_ok:
        blockers.append("nba_api_credential_missing_or_unsafe")
    if not nba_ok:
        blockers.append("nba_account_plan_or_terms_unverified")
    return {
        "read_only": True,
        "ready_for_isolated_nba_only": not blockers,
        "gui_domain_available": domain_ok,
        "sports_job_loaded": job_ok,
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
