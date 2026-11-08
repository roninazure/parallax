"""Check the checked-in REAL NBA model candidates against frozen release gates.

These are the actual 2,460-game model candidates, not synthetic fixtures.
No threshold changes, network access, artifact rewriting, or BUY authorization.
"""
from datetime import datetime
import json
from pathlib import Path

import pytest

from parallax.league_model import verify_artifact

ROOT = Path(__file__).resolve().parents[1] / "reports/nba_nhl_model_candidates"


@pytest.mark.parametrize(
    "filename",
    ["nba_calibrated.json", "nba_v2_candidate.json"],
)
def test_actual_nba_model_artifact_integrity_but_not_certification(filename):
    artifact = json.loads((ROOT / filename).read_text(encoding="utf-8"))
    assert artifact["sport"] == "NBA"
    assert artifact["source_game_count"] == 2460
    assert artifact["splits"]["holdout_games"] == 516
    assert artifact["acceptance_gates"]["beats_half_brier"] is True
    assert artifact["acceptance_gates"]["beats_half_log_loss"] is True
    assert artifact["acceptance_gates"]["beats_half_ece"] is False
    assert artifact["acceptance_gates"]["independent_source_verified"] is False
    assert artifact["acceptance_gates"]["source_authorization_documented"] is False
    assert artifact["acceptance_gates"]["model_owner_approval_documented"] is False
    assert artifact["certified"] is False
    assert artifact["holdout_metrics"]["ece_10_bins"] > artifact["baseline_50_50"]["ece_10_bins"]
    # Artifact digest and gate derivations must be authentic, even when
    # certification fails. Use the frozen generation date to isolate validity
    # from ordinary runtime staleness.
    generated = datetime.fromisoformat(artifact["generated_at"])
    verify_artifact(artifact, sport="NBA", now=generated,
                    require_certified=False)
    with pytest.raises(ValueError, match="certif|pinned|uncertified|exploratory"):
        verify_artifact(artifact, sport="NBA", now=generated,
                        require_certified=True,
                        expected_sha256=artifact["artifact_sha256"])


def test_actual_v2_lacks_fresh_uninspected_forward_holdout():
    artifact = json.loads((ROOT / "nba_v2_candidate.json").read_text(encoding="utf-8"))
    assert artifact["acceptance_gates"]["fresh_forward_holdout"] is False
    assert artifact["splits"]["holdout_status"] == "EXPLORATORY_PREVIOUSLY_INSPECTED"
    assert artifact["independent_source_sha256"] is None
    assert artifact["forward_holdout_source_sha256"] is None


def test_historical_elo_beats_current_v2_but_neither_is_release_eligible():
    elo = json.loads((ROOT / "nba_calibrated.json").read_text(encoding="utf-8"))
    v2 = json.loads((ROOT / "nba_v2_candidate.json").read_text(encoding="utf-8"))
    for metric in ("brier", "log_loss", "ece_10_bins"):
        assert elo["holdout_metrics"][metric] < v2["holdout_metrics"][metric]
    assert elo["certified"] is False
    assert v2["certified"] is False
