"""Synthetic CLI isolation tests; no provider or Mac mini data accessed."""

import json
import sys
from datetime import date

import pytest

from parallax.league_elo_experimental import FinalGame
from scripts import train_league_models as trainer


def _artifact(sport):
    return {
        "artifact_sha256": "a" * 64,
        "certified": False,
        "acceptance_gates": {"independent_source_verified": False},
        "source_game_count": 1,
        "source_seasons": [2025],
        "splits": {"holdout_status": "EXPLORATORY_PREVIOUSLY_INSPECTED"},
        "holdout_metrics": {},
        "baseline_50_50": {},
        "calibration": {},
    }


def _stub_trainer(monkeypatch):
    visited = []

    def history(sport, _path, *, explicit):
        visited.append(sport)
        return [FinalGame(sport, f"{sport}-1", 2025, date(2025, 10, 1), "H", "A", "H")]

    monkeypatch.setattr(trainer, "load_league_history", history)
    monkeypatch.setattr(trainer, "source_digest", lambda sport, home: "a" * 64)
    monkeypatch.setattr(
        trainer, "train_artifact_v2",
        lambda games, *, sport, **kwargs: _artifact(sport),
    )
    return visited


@pytest.mark.parametrize("sport", ["NBA", "NHL"])
def test_single_league_training_does_not_load_other_league(tmp_path, monkeypatch, sport):
    visited = _stub_trainer(monkeypatch)
    output = tmp_path / "artifacts"
    monkeypatch.setattr(
        sys, "argv",
        ["train_league_models.py", "--sport", sport, "--output-dir", str(output)],
    )
    assert trainer.main() == 0
    assert visited == [sport]
    assert (output / f"{sport.lower()}_v2_candidate.json").exists()
    other = "nhl" if sport == "NBA" else "nba"
    assert not (output / f"{other}_v2_candidate.json").exists()
    assert json.loads((output / f"{sport.lower()}_v2_candidate.json").read_text())["certified"] is False


def test_default_training_still_handles_both_leagues(tmp_path, monkeypatch):
    visited = _stub_trainer(monkeypatch)
    monkeypatch.setattr(
        sys, "argv", ["train_league_models.py", "--output-dir", str(tmp_path)],
    )
    assert trainer.main() == 0
    assert visited == ["NBA", "NHL"]


def test_explicit_forward_holdout_without_selected_league_fails_closed(
    tmp_path, monkeypatch
):
    _stub_trainer(monkeypatch)
    monkeypatch.setattr(
        trainer, "load_final_games_csv",
        lambda path: [FinalGame("NHL", "other", 2025, date(2025, 10, 2), "H", "A", "H")],
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "train_league_models.py", "--sport", "NBA",
            "--output-dir", str(tmp_path),
            "--forward-holdout", str(tmp_path / "synthetic.csv"),
        ],
    )
    with pytest.raises(ValueError, match="NBA forward holdout file has no matching"):
        trainer.main()
    assert not (tmp_path / "nba_v2_candidate.json").exists()


def test_both_leagues_failure_leaves_no_partial_candidate_artifacts(
    tmp_path, monkeypatch
):
    """An NHL validation failure cannot publish an NBA-only batch."""
    visited = _stub_trainer(monkeypatch)

    def train(games, *, sport, **kwargs):
        if sport == "NHL":
            raise ValueError("synthetic NHL calibration failure")
        return _artifact(sport)

    monkeypatch.setattr(trainer, "train_artifact_v2", train)
    output = tmp_path / "batch"
    monkeypatch.setattr(
        sys, "argv", ["train_league_models.py", "--output-dir", str(output)]
    )
    with pytest.raises(ValueError, match="synthetic NHL calibration failure"):
        trainer.main()
    assert visited == ["NBA", "NHL"]
    assert not output.exists()
