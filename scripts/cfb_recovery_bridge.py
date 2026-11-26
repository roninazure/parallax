#!/usr/bin/env python3
"""Temporary independent CFB lane for recovering CFB without restarting NFL."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        temporary = Path(f.name)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--runtime-env", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()

    release = args.release.resolve(strict=True)
    executable = release / "scripts/cfb_live_scan.py"
    if not executable.is_file():
        raise SystemExit("BLOCKED: CFB executable unavailable")
    if not args.runtime_env.is_file():
        raise SystemExit("BLOCKED: runtime.env unavailable")
    sys.path.insert(0, str(release))
    from scripts.parallax_unattended import load_runtime_env
    from parallax.alerts import AlertConfig
    from parallax.public_feed import export_completed_scan

    runtime_values = load_runtime_env(args.runtime_env)
    os.environ.update(runtime_values)
    config = AlertConfig.load()
    if config.mode not in {"ntfy", "webhook"}:
        raise SystemExit("BLOCKED: real CFB alerts are not configured (disabled or dry_run)")
    if config.mode == "ntfy" and not config.ntfy_topic:
        raise SystemExit("BLOCKED: ntfy topic missing")
    if config.mode == "webhook" and not config.webhook_url:
        raise SystemExit("BLOCKED: webhook URL missing")
    if args.preflight:
        print("CFB BRIDGE PREFLIGHT PASS: real alert delivery configured; NFL untouched")
        return 0

    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=True)
    lock_path = state / "cfb_recovery_bridge.lock"
    phase_path = state / "cfb_recovery_bridge_phase.json"
    atomic_json(phase_path, {
        "status": "STARTING",
        "updated_at": datetime.now(UTC).isoformat(),
        "release": release.name,
    })
    print("CFB BRIDGE: entering independent scan", flush=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("CFB bridge: another scan is already active")
            return 0
        env = os.environ.copy()
        env["PYTHONPATH"] = str(release)
        env["PARALLAX_PUBLIC_FEED_STATE_DIR"] = str(state)
        atomic_json(phase_path, {
            "status": "SCANNING",
            "updated_at": datetime.now(UTC).isoformat(),
            "release": release.name,
        })
        print("CFB BRIDGE: live scan started; scanner output captured until completion", flush=True)
        result = subprocess.run(
            [sys.executable, str(executable)],
            cwd=state,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if result.returncode:
            raise SystemExit(f"CFB scan failed (exit {result.returncode}): {(result.stderr or '')[:300]}")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or payload.get("read_only") is not True or payload.get("orders") != 0:
            raise SystemExit("BLOCKED: CFB safety invariant failed")
        if not isinstance(payload.get("rows"), list) or not payload["rows"]:
            raise SystemExit("BLOCKED: no scored CFB rows")
        eligible = sum(row.get("publication_eligible") is True for row in payload["rows"])
        statuses = [
            info.get("status_counts", {})
            for info in payload.get("venues", {}).values()
            if isinstance(info, dict)
        ]
        delivery_errors = sum(int(counts.get("ALERT_ERROR", 0)) for counts in statuses)
        deduplicated = sum(int(counts.get("ALERT_DEDUPLICATED", 0)) for counts in statuses)
        sent = int(payload.get("alerts", 0))
        if delivery_errors:
            raise SystemExit(f"BLOCKED: {delivery_errors} CFB notification errors")
        if eligible and sent + deduplicated == 0:
            raise SystemExit("BLOCKED: eligible CFB BUYs were neither notified nor deduplicated")
        export_completed_scan("cfb", result.stdout, state)
        health = {
            "status": "PASS",
            "scanned_at": datetime.now(UTC).isoformat(),
            "release": release.name,
            "scored_positions": len(payload["rows"]),
            "buy_rows": eligible,
            "alerts_sent": sent,
            "alerts_deduplicated": deduplicated,
            "feed": str(state / "public_feed/cfb.json"),
        }
        atomic_json(state / "cfb_recovery_bridge_health.json", health)
        atomic_json(phase_path, {**health, "status": "PASS"})
        print(json.dumps(health, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Avoid printing runtime environment or transport credentials to a launchd log.
        print(f"CFB BRIDGE FAILED: {type(exc).__name__}", file=sys.stderr, flush=True)
        raise
