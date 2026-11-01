"""Read-only NBA Elo candidate comparison; requires separate TRAIN/CAL CSVs.

No holdout argument exists. No provider calls, production writes, or BUY output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

from parallax.league_elo_experimental import load_final_games_csv
from parallax.nba_elo_calibration_candidate import evaluate_nba_elo_candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--train-end", type=date.fromisoformat, required=True)
    parser.add_argument("--calibration-end", type=date.fromisoformat, required=True)
    parser.add_argument("--home-advantage", type=float, required=True)
    parser.add_argument("--k", type=float, required=True)
    args = parser.parse_args(argv)
    if args.train.resolve() == args.calibration.resolve():
        parser.error("TRAIN and CALIBRATION must be separate files")
    train_sha = hashlib.sha256(args.train.read_bytes()).hexdigest()
    cal_sha = hashlib.sha256(args.calibration.read_bytes()).hexdigest()
    train = load_final_games_csv(args.train)
    calibration = load_final_games_csv(args.calibration)
    if (hashlib.sha256(args.train.read_bytes()).hexdigest() != train_sha
            or hashlib.sha256(args.calibration.read_bytes()).hexdigest() != cal_sha):
        raise ValueError("input changed during parsing")
    result = evaluate_nba_elo_candidate(
        train, calibration, train_end=args.train_end,
        calibration_end=args.calibration_end,
        home_advantage=args.home_advantage, k=args.k,
    )
    result["input_sha256"] = {"train": train_sha, "calibration": cal_sha}
    result["source_authorization_verified"] = False
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
