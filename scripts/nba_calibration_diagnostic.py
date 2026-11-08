#!/usr/bin/env python3
"""Read-only NBA reliability diagnostics for an ALREADY INSPECTED holdout.

No model selection, artifact writes, network access, certification or BUY authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from parallax.league_elo_experimental import load_final_games_csv
from parallax.league_model import (
    _apply_calibration, _metrics, _replay, verify_artifact,
    verify_history_snapshot,
)


def summarize(ps: list[float], ys: list[int]) -> dict:
    n = len(ps)
    return {
        "games": n,
        "mean_predicted_home_win": sum(ps) / n,
        "actual_home_win_rate": sum(ys) / n,
        "calibration_gap": sum(ps) / n - sum(ys) / n,
        "brier": _metrics(ps, ys)["brier"],
        "log_loss": _metrics(ps, ys)["log_loss"],
        "ece_10_bins": _metrics(ps, ys)["ece_10_bins"],
    }


def diagnose(history_path: Path, artifact_path: Path) -> dict:
    if history_path.is_symlink() or artifact_path.is_symlink():
        raise ValueError("symlinks not permitted")
    source_sha = hashlib.sha256(history_path.read_bytes()).hexdigest()
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    if artifact.get("sport") != "NBA" or artifact.get("source_sha256") != source_sha:
        raise ValueError("wrong league or frozen source digest mismatch")
    verify_artifact(artifact, sport="NBA", now=datetime.fromisoformat(artifact["generated_at"]), require_certified=False)
    rows = load_final_games_csv(history_path)
    if hashlib.sha256(history_path.read_bytes()).hexdigest() != source_sha:
        raise ValueError("source changed while reading")
    verify_history_snapshot(rows, artifact, sport="NBA")
    if artifact.get("certified") is not False:
        raise ValueError("diagnostic requires an uncertified research artifact")

    splits = artifact["splits"]
    holdout = [g for g in rows if g.game_date.isoformat() > splits["calibration_end"]]
    if len(holdout) != splits["holdout_games"]:
        raise ValueError("holdout split mismatch")
    raw, _ = _replay(rows, float(artifact["elo"]["home_advantage"]), float(artifact["elo"]["k"]))
    ps = [_apply_calibration(artifact["calibration"], raw[("NBA", g.game_id)]) for g in holdout]
    ys = [int(g.winner_team == g.home_team) for g in holdout]
    metrics = _metrics(ps, ys)
    if any(abs(metrics[k] - artifact["holdout_metrics"][k]) > 1e-10
           for k in ("brier", "log_loss", "ece_10_bins")):
        raise ValueError("replayed holdout metrics diverge from frozen artifact")

    bands: dict[int, list[tuple[float, int]]] = defaultdict(list)
    seasons: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for g, p, y in zip(holdout, ps, ys, strict=True):
        bands[min(9, int(p * 10))].append((p, y))
        seasons[g.season].append((p, y))

    def breakdown(groups):
        return {str(key): summarize([p for p, _ in values], [y for _, y in values])
                for key, values in sorted(groups.items())}

    return {
        "status": "DIAGNOSTIC_ONLY_PREVIOUSLY_INSPECTED_HOLDOUT",
        "source_sha256": source_sha,
        "artifact_sha256": artifact["artifact_sha256"],
        "calibration_method": artifact["calibration"]["method"],
        "overall": summarize(ps, ys),
        "probability_bands": breakdown(bands),
        "band_definition": "0 means [0.0,0.1), ..., 9 means [0.9,1.0]",
        "by_season": breakdown(seasons),
        "model_certified": False,
        "buy_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(diagnose(args.history, args.baseline), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
