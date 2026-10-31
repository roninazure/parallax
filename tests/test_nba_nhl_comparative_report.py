"""Synthetic-only calibration comparison report checks."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.nba_nhl_comparative_report import compare

ROOT = Path(__file__).resolve().parents[1] / "reports/nba_nhl_model_candidates"


def _signed(value):
    value = copy.deepcopy(value)
    unsigned = {k: v for k, v in value.items() if k != "artifact_sha256"}
    value["artifact_sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                   allow_nan=False).encode()
    ).hexdigest()
    return value


@pytest.mark.parametrize("sport", ["nba", "nhl"])
def test_checked_in_candidate_is_exploratory_and_non_authorizing(sport):
    artifact = json.loads((ROOT / f"{sport}_v2_candidate.json").read_text())
    report = compare(artifact)
    assert report["sport"] == sport.upper()
    assert report["holdout_status"] == "EXPLORATORY_PREVIOUSLY_INSPECTED"
    assert report["production_buy_authorized"] is False
    assert report["interpretation"] == "EXPLORATORY_NON_CERTIFYING"
    assert report["candidate_minus_baselines"]["baseline_50_50"]["ece_10_bins"] > 0
    assert report["holdout_n_games"] > 0


def test_tampered_artifact_rejected():
    artifact = json.loads((ROOT / "nba_v2_candidate.json").read_text())
    artifact["holdout_metrics"]["brier"] = 0.001
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        compare(artifact)


def test_mismatched_comparison_population_rejected():
    artifact = json.loads((ROOT / "nba_v2_candidate.json").read_text())
    artifact["model_comparison"]["elo_calibrated_same_splits"]["n_games"] -= 1
    with pytest.raises(ValueError, match="different/empty"):
        compare(_signed(artifact))


def test_missing_baseline_rejected():
    artifact = json.loads((ROOT / "nba_v2_candidate.json").read_text())
    artifact["model_comparison"].pop("constant_home_rate_fit_on_train")
    with pytest.raises(ValueError, match="invalid metric count"):
        compare(_signed(artifact))


def test_unknown_holdout_provenance_rejected():
    artifact = json.loads((ROOT / "nba_v2_candidate.json").read_text())
    artifact["splits"]["holdout_status"] = "CERTIFIED"
    with pytest.raises(ValueError, match="unknown holdout provenance"):
        compare(_signed(artifact))
