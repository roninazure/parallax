#!/usr/bin/env python3
"""Convert locally saved NBA/NHL provider JSON to experimental CSV.

No network operations, production state, alerts, or model certification.
Only use snapshots obtained and stored with appropriate data rights.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from parallax.league_snapshot_adapters import (
    export_canonical_csv, merge_deduplicated,
    nba_from_balldontlie, nhl_from_club_schedule,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nba-json", action="append", type=Path, default=[],
                        help="Saved BALLDONTLIE /v1/games JSON response (repeat for pages)")
    parser.add_argument("--nhl-json", action="append", type=Path, default=[],
                        help="Saved NHL club-schedule JSON (repeat for teams/seasons)")
    parser.add_argument("--nba-regular-query-verified", action="store_true",
                        help="Confirm each NBA snapshot came from a season_type=regular query")
    parser.add_argument("--output", type=Path, required=True,
                        help="Destination canonical local CSV; never a production feed path")
    args = parser.parse_args()
    if not args.nba_json and not args.nhl_json:
        parser.error("At least one --nba-json or --nhl-json file is required")
    if args.nba_json and not args.nba_regular_query_verified:
        parser.error("NBA requires --nba-regular-query-verified")
    sources = []
    for path in args.nba_json:
        sources.append(nba_from_balldontlie(json.loads(path.read_text(encoding="utf-8")),
            regular_season_query_verified=args.nba_regular_query_verified))
    for path in args.nhl_json:
        sources.append(nhl_from_club_schedule(json.loads(path.read_text(encoding="utf-8"))))
    games = merge_deduplicated(*sources)
    if not games:
        parser.error("No final regular-season games found in snapshots")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    export_canonical_csv(args.output, games)
    counts = {sport: sum(g.sport == sport for g in games) for sport in ("NBA", "NHL")}
    print(json.dumps({"status": "OFFLINE_EXPERIMENTAL_ONLY", "output": str(args.output),
                      "final_games": len(games), "by_sport": counts}, sort_keys=True))


if __name__ == "__main__":
    main()
