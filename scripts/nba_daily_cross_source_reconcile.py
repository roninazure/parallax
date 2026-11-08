#!/usr/bin/env python3
"""Offline NBA ESPN-vs-BALLDONTLIE final-result reconciliation.

Research-only historical evidence. No network, writes, certification or BUY.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import date
from pathlib import Path

from parallax.league_elo_experimental import load_final_games_csv
from scripts.nba_forward_reconcile import canonical


def compare(espn_csv: Path, provenance_file: Path, bdl_file: Path) -> dict:
    if any(p.is_symlink() for p in (espn_csv, provenance_file, bdl_file)):
        raise ValueError("symlink inputs prohibited")
    espn_sha = hashlib.sha256(espn_csv.read_bytes()).hexdigest()
    provenance = json.loads(provenance_file.read_text(encoding="utf-8"))
    if (provenance.get("csv_sha256") != espn_sha
            or provenance.get("source") != "ESPN_PUBLIC_SCOREBOARD_RESEARCH_ONLY"):
        raise ValueError("ESPN snapshot provenance invalid")
    espn = load_final_games_csv(espn_csv)
    bdl_sha = hashlib.sha256(bdl_file.read_bytes()).hexdigest()
    bdl = json.loads(bdl_file.read_text(encoding="utf-8"))
    raw = bdl.get("data") if isinstance(bdl, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ValueError("BALLDONTLIE final-results array missing")
    expected = {}
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("malformed BALLDONTLIE game")
        day = date.fromisoformat(row["date"][:10])
        home = canonical(row["home_team"]["abbreviation"])
        away = canonical(row["visitor_team"]["abbreviation"])
        hs, vs = row["home_team_score"], row["visitor_team_score"]
        if (row.get("season") != 2026 or row.get("status_state") != "final"
                or row.get("postseason") is not False or row.get("postponed") is not False
                or type(hs) is not int or type(vs) is not int
                or hs < 0 or vs < 0 or hs == vs or not home or not away or home == away):
            raise ValueError("ineligible BALLDONTLIE final")
        key = (day, away, home)
        if key in expected:
            raise ValueError("duplicate BALLDONTLIE fixture")
        expected[key] = home if hs > vs else away
    start, end = min(k[0] for k in expected), max(k[0] for k in expected)
    actual = {}
    for game in espn:
        if game.sport != "NBA" or game.season != 2026:
            raise ValueError("non-NBA 2026 ESPN row")
        if not start <= game.game_date <= end:
            continue
        key = (game.game_date, canonical(game.away_team), canonical(game.home_team))
        if key in actual:
            raise ValueError("duplicate ESPN fixture")
        actual[key] = canonical(game.winner_team)
    missing = sorted(expected.keys() - actual.keys())
    unexpected = sorted(actual.keys() - expected.keys())
    disagreements = sorted(k for k in expected.keys() & actual.keys() if expected[k] != actual[k])
    counts = Counter(k[0].isoformat() for k in expected)
    observed = Counter(k[0].isoformat() for k in actual)
    daily = [{"date": d, "bdl_finals": counts[d], "espn_finals": observed[d],
              "matched": counts[d] == observed[d] and not any(k[0].isoformat() == d for k in missing + unexpected + disagreements)}
             for d in sorted(set(counts) | set(observed))]
    matched = not (missing or unexpected or disagreements)
    def labels(keys):
        return [{"date": d.isoformat(), "away": away, "home": home} for d, away, home in keys]
    return {
        "status": "RESEARCH_SOURCES_MATCH" if matched else "RESEARCH_RECONCILIATION_BLOCKED",
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "espn_sha256": espn_sha, "bdl_sha256": bdl_sha,
        "expected_games": len(expected), "observed_games": len(actual),
        "daily": daily, "missing": labels(missing), "unexpected": labels(unexpected),
        "winner_disagreements": labels(disagreements),
        "cross_source_results_consistent": matched,
        "independent_provider_authorization_verified": False,
        "full_schedule_coverage_verified": False,
        "model_certified": False, "buy_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--espn-csv", type=Path, required=True)
    parser.add_argument("--espn-provenance", type=Path, required=True)
    parser.add_argument("--bdl-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Optional write-once local evidence JSON")
    args = parser.parse_args()
    report = compare(args.espn_csv, args.espn_provenance, args.bdl_json)
    encoded = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if args.output is not None:
        if args.output.is_symlink() or args.output.exists():
            parser.error("output exists or is a symlink; refusing overwrite")
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    print(encoded.decode("utf-8"), end="")
    return 0 if report["cross_source_results_consistent"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
