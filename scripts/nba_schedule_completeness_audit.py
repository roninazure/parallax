#!/usr/bin/env python3
"""Offline NBA schedule-coverage audit. Research only; never certifies BUY.

Requires a separately obtained schedule fixture CSV and provenance. Does not
claim complete coverage or independent authorization from agreement alone.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections import Counter
from datetime import date
from pathlib import Path

from parallax.league_elo_experimental import load_final_games_csv
from scripts.nba_daily_cross_source_reconcile import compare
from scripts.nba_forward_reconcile import canonical

FIELDS = ("game_date", "home_team", "away_team", "status")
STATUSES = {"FINAL", "POSTPONED", "CANCELLED", "UNRESOLVED"}


def audit(espn: Path, espn_provenance: Path, bdl: Path,
          schedule: Path | None = None, schedule_provenance: Path | None = None) -> dict:
    sources = (espn, espn_provenance, bdl)
    if any(p.is_symlink() for p in sources):
        raise ValueError("symlink inputs prohibited")
    agreement = compare(*sources)
    result = {
        "status": "SCHEDULE_SOURCE_REQUIRED",
        "network_requests": 0,
        "cross_source_results_consistent": agreement["cross_source_results_consistent"],
        "cross_source_games": agreement["observed_games"],
        "schedule_source_supplied": False,
        "fixture_coverage_complete": False,
        "full_schedule_coverage_verified": False,
        "source_authorization_verified": False,
        "model_certified": False,
        "buy_authorized": False,
    }
    if schedule is None and schedule_provenance is None:
        return result
    if schedule is None or schedule_provenance is None:
        raise ValueError("schedule and provenance must be provided together")
    if schedule.is_symlink() or schedule_provenance.is_symlink():
        raise ValueError("schedule symlinks prohibited")
    data = schedule.read_bytes()
    manifest = json.loads(schedule_provenance.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("invalid schedule provenance")
    if manifest.get("csv_sha256") != hashlib.sha256(data).hexdigest():
        raise ValueError("schedule hash mismatch")
    if not isinstance(manifest.get("schedule_source"), str) or not manifest["schedule_source"].strip():
        raise ValueError("schedule source not named")
    start = date.fromisoformat(manifest["coverage_start"])
    end = date.fromisoformat(manifest["coverage_end"])
    if start > end or (end - start).days > 365:
        raise ValueError("invalid bounded schedule coverage window")
    with schedule.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(FIELDS):
            raise ValueError("schedule must have exact required columns")
        expected = {}
        duplicates = []
        for row in reader:
            day = date.fromisoformat(row["game_date"])
            home, away = canonical(row["home_team"]), canonical(row["away_team"])
            status = row["status"].strip().upper()
            if not start <= day <= end or not home or not away or home == away or status not in STATUSES:
                raise ValueError("invalid schedule fixture")
            key = (day, away, home)
            if key in expected:
                duplicates.append(key)
            expected[key] = status
    if not expected:
        raise ValueError("empty schedule")
    finals = {}
    for game in load_final_games_csv(espn):
        if game.sport != "NBA" or game.season != 2026:
            raise ValueError("unexpected ESPN game")
        if not start <= game.game_date <= end:
            continue
        key = (game.game_date, canonical(game.away_team), canonical(game.home_team))
        if key in finals:
            raise ValueError("duplicate ESPN fixture")
        finals[key] = canonical(game.winner_team)
    expected_finals = {k for k, v in expected.items() if v == "FINAL"}
    missing_finals = expected_finals - finals.keys()
    unexpected_finals = finals.keys() - expected_finals
    nonfinal = {k for k, v in expected.items() if v != "FINAL"}
    counts = Counter(expected.values())
    def label(keys):
        return [{"date": d.isoformat(), "away": a, "home": h}
                for d, a, h in sorted(keys)]
    consistent = (not duplicates and not missing_finals and not unexpected_finals
                  and agreement["cross_source_results_consistent"])
    # A self-supplied provenance assertion does not independently establish
    # provider permission, nor that the supplied schedule is authoritative.
    result.update({
        "status": "RESEARCH_FIXTURE_COUNTS_MATCH_UNVERIFIED"
                  if consistent else "RESEARCH_SCHEDULE_RECONCILIATION_BLOCKED",
        "schedule_source_supplied": True,
        "schedule_source": manifest["schedule_source"],
        "schedule_sha256": hashlib.sha256(data).hexdigest(),
        "coverage_start": start.isoformat(), "coverage_end": end.isoformat(),
        "schedule_fixtures": len(expected), "final_fixtures": len(expected_finals),
        "postponed": counts["POSTPONED"], "cancelled": counts["CANCELLED"],
        "unresolved": counts["UNRESOLVED"],
        "duplicates": label(duplicates),
        "missing_finals": label(missing_finals),
        "unexpected_finals": label(unexpected_finals),
        "nonfinal_fixtures": label(nonfinal),
        "fixture_coverage_complete": consistent,
        "provenance_claims_authorization": manifest.get("source_authorization_verified") is True,
        "full_schedule_coverage_verified": False,
        "source_authorization_verified": False,
    })
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--espn-csv", type=Path, required=True)
    p.add_argument("--espn-provenance", type=Path, required=True)
    p.add_argument("--bdl-json", type=Path, required=True)
    p.add_argument("--schedule-csv", type=Path)
    p.add_argument("--schedule-provenance", type=Path)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    report = audit(args.espn_csv, args.espn_provenance, args.bdl_json,
                   args.schedule_csv, args.schedule_provenance)
    encoded = (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    if args.output:
        if args.output.is_symlink() or args.output.exists():
            p.error("refusing overwrite or symlink")
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    print(encoded.decode(), end="")
    return 0 if report["fixture_coverage_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
