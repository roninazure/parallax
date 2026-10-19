#!/usr/bin/env python3
"""Offline NBA/NHL historical baseline report. No API calls, no BUYs."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from parallax.league_elo_experimental import (
    holdout_metrics, load_final_games_csv, replay,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("historical_csv", help="Verified local final results")
    parser.add_argument("--holdout-season", type=int, required=True)
    args = parser.parse_args()
    predictions = replay(load_final_games_csv(args.historical_csv))
    report = {
        "certification": "EXPERIMENTAL_NOT_CERTIFIED",
        "live_requests": 0,
        "sports": [asdict(holdout_metrics(
            predictions, sport=sport, season=args.holdout_season
        )) for sport in ("NBA", "NHL")],
    }
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
