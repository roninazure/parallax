"""Read-only comparison of the active league plist, heartbeat, and manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any


def _launchd_job(label: str) -> dict[str, Any]:
    """Resolve the actual launchd domain without changing running services."""
    uid = os.getuid()
    for domain in ("system", f"gui/{uid}", f"user/{uid}"):
        try:
            result = subprocess.run(
                ["launchctl", "print", f"{domain}/{label}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "error": type(exc).__name__}
        if result.returncode != 0:
            continue
        safe_lines = [
            line.strip()
            for line in result.stdout.splitlines()
            if re.match(r"\\s*(state|pid|last exit code|program|path)\\s*=", line)
        ]
        return {
            "available": True,
            "domain": domain,
            "summary": safe_lines,
            "error": None,
        }
    return {"available": False, "domain": None, "summary": None,
            "error": "launchctl_lookup_failed"}

def diagnose(args: argparse.Namespace) -> int:
    root = Path(args.runtime_root).expanduser()
    manifest_path = (
        Path(args.manifest).expanduser()
        if args.manifest
        else root / "run/parallax-sports-manifest.json"
    )
    plist_paths = sorted(Path(args.plist_dir).expanduser().glob(args.plist_pattern))
    report: dict[str, Any] = {
        "read_only": True,
        "runtime_root": str(root),
        "manifest_path": str(manifest_path),
        "plists": [],
        "mismatches": [],
    }
    plist_records = []
    for path in plist_paths:
        try:
            raw = path.read_bytes()
            value = plistlib.loads(raw)
            args_list = value.get("ProgramArguments", [])
            wd = value.get("WorkingDirectory")
            release_path = next(
                (v for v in args_list if isinstance(v, str) and "/releases/" in v), None
            )
            entrypoint = next(
                (
                    v
                    for v in args_list
                    if isinstance(v, str)
                    and v.endswith("/scripts/parallax_unattended.py")
                ),
                None,
            )
            runtime_env_path = None
            state_dir = None
            if "--runtime-env" in args_list:
                index = args_list.index("--runtime-env")
                if index + 1 < len(args_list):
                    runtime_env_path = args_list[index + 1]
            if "--state-dir" in args_list:
                index = args_list.index("--state-dir")
                if index + 1 < len(args_list):
                    state_dir = args_list[index + 1]
            release_sha = Path(release_path).parents[1].name if release_path else None
            record = {
                "path": str(path),
                "plist_sha256": hashlib.sha256(raw).hexdigest(),
                "label": value.get("Label"),
                "working_directory": wd,
                "entrypoint": entrypoint,
                "entrypoint_sha256": _file_sha(Path(entrypoint))
                if entrypoint and Path(entrypoint).is_file()
                else None,
                "runtime_config_sha256": _file_sha(Path(runtime_env_path))
                if runtime_env_path and Path(runtime_env_path).is_file()
                else None,
                "state_dir": state_dir,
                "release_sha_from_argument": release_sha,
            }
            plist_records.append(record)
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            plist_records.append({"path": str(path), "error": type(exc).__name__})
    report["plists"] = plist_records
    if not plist_paths:
        report["mismatches"].append("no_matching_launchd_plist")
    health_path = (
        Path(args.health).expanduser()
        if args.health
        else Path(
            next(
                (row.get("state_dir") for row in plist_records if row.get("state_dir")),
                root / "state/parallax",
            )
        )
        / "parallax_unattended_health.json"
    )
    try:
        health = json.loads(health_path.read_text(encoding="utf-8"))
        report["health"] = {
            "path": str(health_path),
            "state": health.get("state"),
            "release_sha": health.get("release_sha"),
            "last_heartbeat": health.get("last_heartbeat"),
        }
    except (OSError, json.JSONDecodeError) as exc:
        health = {}
        report["health"] = {"path": str(health_path), "error": type(exc).__name__}
        report["mismatches"].append("health_file_missing_or_invalid")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report["manifest"] = {
            "path": str(manifest_path),
            "git_sha": manifest.get("git_sha"),
            "launchd_plist_sha256": manifest.get("launchd_plist_sha256"),
            "runtime_config_sha256": manifest.get("runtime_config_sha256"),
            "runtime_entrypoint_sha256": manifest.get("runtime_entrypoint_sha256"),
        }
    except (OSError, json.JSONDecodeError) as exc:
        manifest = {}
        report["manifest"] = {"path": str(manifest_path), "error": type(exc).__name__}
        report["mismatches"].append("manifest_missing_or_invalid")
    plist_sha = {
        row.get("plist_sha256") for row in plist_records if row.get("plist_sha256")
    }
    expected_plist = manifest.get("launchd_plist_sha256")
    if expected_plist and plist_sha and expected_plist not in plist_sha:
        report["mismatches"].append("manifest_launchd_plist_sha_mismatch")
    entrypoint_shas = {
        row.get("entrypoint_sha256")
        for row in plist_records
        if row.get("entrypoint_sha256")
    }
    if (
        manifest.get("runtime_entrypoint_sha256")
        and entrypoint_shas
        and manifest["runtime_entrypoint_sha256"] not in entrypoint_shas
    ):
        report["mismatches"].append("manifest_runtime_entrypoint_sha_mismatch")
    config_shas = {
        row.get("runtime_config_sha256")
        for row in plist_records
        if row.get("runtime_config_sha256")
    }
    if (
        manifest.get("runtime_config_sha256")
        and config_shas
        and manifest["runtime_config_sha256"] not in config_shas
    ):
        report["mismatches"].append("manifest_runtime_config_sha_mismatch")
    release_ids = {
        row.get("release_sha_from_argument")
        for row in plist_records
        if row.get("release_sha_from_argument")
    }
    if (
        manifest.get("git_sha")
        and release_ids
        and manifest["git_sha"] not in release_ids
    ):
        report["mismatches"].append("manifest_git_sha_differs_from_plist_release")
    if (
        health.get("release_sha")
        and release_ids
        and health["release_sha"] not in release_ids
    ):
        report["mismatches"].append("heartbeat_release_sha_differs_from_plist")
    label = next(
        (row.get("label") for row in plist_records if row.get("label")),
        "com.swarmedge.parallax-sports",
    )
    report["launchd"] = _launchd_job(label)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["mismatches"] else 0


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-root", default="~/Library/Application Support/SwarmEdge"
    )
    parser.add_argument("--manifest")
    parser.add_argument("--health")
    parser.add_argument(
        "--plist-dir", default="~/Library/Application Support/SwarmEdge/staging"
    )
    parser.add_argument(
        "--plist-pattern", default="com.swarmedge.parallax-sports.*.plist"
    )
    return diagnose(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
