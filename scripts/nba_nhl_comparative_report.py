"""Offline comparative NBA/NHL calibration report for exploratory artifacts.

Read-only: no network, runtime actions, model certification, or BUY authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

METRICS = ("brier", "log_loss", "ece_10_bins")
BASELINES = ("baseline_50_50", "elo_calibrated_same_splits",
             "constant_home_rate_fit_on_train")


def _integrity(artifact: dict) -> None:
    digest = artifact.get("artifact_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("artifact SHA-256 missing")
    unsigned = {k: v for k, v in artifact.items() if k != "artifact_sha256"}
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode()
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise ValueError("artifact SHA-256 mismatch")


def _metric_set(value: object, expected_n: int) -> dict[str, float]:
    if not isinstance(value, dict) or type(value.get("n_games")) is not int:
        raise ValueError("invalid metric count")
    if value["n_games"] != expected_n or expected_n <= 0:
        raise ValueError("comparison metrics use different/empty holdout samples")
    output = {}
    for key in METRICS:
        v = value.get(key)
        if type(v) not in (float, int) or not math.isfinite(v) or v < 0:
            raise ValueError("invalid metric " + key)
        output[key] = float(v)
    return output


def compare(artifact: dict) -> dict:
    if not isinstance(artifact, dict):
        raise ValueError("model artifact must be an object")
    _integrity(artifact)
    sport = artifact.get("sport")
    if sport not in ("NBA", "NHL") or artifact.get("schema_version") != 2:
        raise ValueError("expected NBA/NHL v2 artifact")
    comparison = artifact.get("model_comparison")
    if not isinstance(comparison, dict):
        raise ValueError("missing model comparison")
    holdout = artifact.get("splits")
    if not isinstance(holdout, dict):
        raise ValueError("missing holdout provenance")
    n = artifact.get("holdout_metrics", {}).get("n_games")
    if type(n) is not int or n <= 0:
        raise ValueError("invalid holdout size")
    candidate = _metric_set(artifact["holdout_metrics"], n)
    if _metric_set(comparison.get("selected_v2"), n) != candidate:
        raise ValueError("selected v2 comparison differs from holdout metrics")
    sources = {"baseline_50_50": artifact.get("baseline_50_50")}
    sources.update({name: comparison.get(name) for name in BASELINES[1:]})
    deltas = {}
    for name, data in sources.items():
        baseline = _metric_set(data, n)
        deltas[name] = {key: round(candidate[key] - baseline[key], 12)
                        for key in METRICS}
    gates = artifact.get("acceptance_gates")
    if not isinstance(gates, dict) or not gates:
        raise ValueError("missing acceptance gates")
    status = holdout.get("holdout_status")
    if status not in ("FRESH_INDEPENDENT", "EXPLORATORY_PREVIOUSLY_INSPECTED"):
        raise ValueError("unknown holdout provenance")
    return {
        "sport": sport,
        "source_artifact_sha256": artifact["artifact_sha256"],
        "holdout_n_games": n,
        "holdout_status": status,
        "candidate_metrics": candidate,
        "candidate_minus_baselines": deltas,
        "acceptance_gates": gates,
        # This report is diagnostic; no report can itself grant production approval.
        "production_buy_authorized": False,
        "interpretation": "EXPLORATORY_NON_CERTIFYING",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        reports = [compare(json.loads(path.read_text(encoding="utf-8")))
                   for path in args.artifacts]
        if len({x["sport"] for x in reports}) != len(reports):
            raise ValueError("duplicate sport in comparison")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(1, "COMPARISON_FAILED: " + str(exc) + "\n")
    print(json.dumps(reports, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
