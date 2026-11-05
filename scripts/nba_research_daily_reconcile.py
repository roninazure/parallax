#!/usr/bin/env python3
"""NBA-only unattended research scorecard, one bounded attempt per ET day.

Reads existing archived forecasts and public NBA finals. Never touches a
production lane, prediction model, market API, alert or publication feed.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nba_forward_reconcile import archived_forecasts, evaluate, parse_stamp

ET = ZoneInfo("America/New_York")


def _write_json_exclusive(path: Path, document: dict) -> None:
    data = (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def run(*, snapshots: Path, state: Path, now: datetime, scorer=evaluate) -> dict:
    if now.tzinfo is None:
        raise ValueError("aware now required")
    if not snapshots.is_dir():
        raise ValueError("NBA forecast directory missing")
    if state.is_symlink():
        raise ValueError("state directory must not be a symlink")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    day = now.astimezone(ET).strftime("%Y%m%d")
    lock_path = state / "nba-reconcile.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "SKIPPED_CONCURRENT", "day": day}
        attempt = state / f"attempt-{day}.json"
        report = state / f"scorecard-{day}.json"
        if attempt.exists() or report.exists():
            return {"status": "SKIPPED_ALREADY_ATTEMPTED", "day": day}
        forecasts, source = archived_forecasts(snapshots)
        if not forecasts:
            raise ValueError("no valid NBA forecasts")
        if any(source.get(k, 0) for k in ("malformed_snapshots", "invalid_rows")):
            raise ValueError("invalid NBA snapshot data; report not trusted")
        # Reserve BEFORE making any network requests; failure cannot cause
        # repeated paid/unpaid provider calls from unattended restarts.
        _write_json_exclusive(attempt, {"day": day, "started_at_utc": now.astimezone(UTC).isoformat(),
                                        "status": "ATTEMPT_RESERVED", "research_only": True})
        result = scorer(forecasts, now=now)
        if result["scoreboard_errors"]:
            raise RuntimeError("scoreboard lookup failed; no scorecard published")
        if result["graded_games"] == 0:
            raise RuntimeError("no graded NBA games; no scorecard published")
        if result["unique_forecasts"] != result["graded_games"] + len(result["unmatched"]):
            raise RuntimeError("scorecard coverage mismatch")
        # An apparently completed matchup without a score must be visible as
        # an exception rather than silently reported as fully reconciled.
        result["source_counts"] = source
        result["generated_at_utc"] = now.astimezone(UTC).isoformat()
        result["day"] = day
        result["run_status"] = (
            "COMPLETE_FOR_COMPLETED_MATCHES"
            if not result["unmatched_counts"].get("MISSING_FINAL_OR_IDENTITY")
            else "PARTIAL_MISSING_FINAL_OR_IDENTITY"
        )
        _write_json_exclusive(report, result)
        return {"status": result["run_status"], "day": day,
                "graded_games": result["graded_games"], "correct": result["correct"],
                "accuracy": result["accuracy"], "brier": result["brier"],
                "ece_10_bins": result["ece_10_bins"],
                "unmatched_counts": result["unmatched_counts"], "report": str(report)}
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    try:
        status = run(snapshots=args.snapshots, state=args.state,
                     now=datetime.now(UTC))
        print(json.dumps(status, sort_keys=True, allow_nan=False), flush=True)
        if status["status"] == "PARTIAL_MISSING_FINAL_OR_IDENTITY":
            return 2
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"NBA_RESEARCH_RECONCILIATION_FAILED: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
