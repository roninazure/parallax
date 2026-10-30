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
    (tmp_path / "synthetic.csv").write_text("synthetic\n", encoding="utf-8")
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


@pytest.mark.parametrize("evidence", ["forward_holdout", "independent_history"])
def test_external_evidence_modified_while_parsing_fails_closed(
    tmp_path, monkeypatch, evidence
):
    """The stored evidence digest must refer to the bytes actually parsed."""
    _stub_trainer(monkeypatch)
    path = tmp_path / "evidence.csv"
    path.write_text("snapshot-before-parsing\n", encoding="utf-8")
    output = tmp_path / "output"
    def mutate_during_parse(source):
        with source.open("a", encoding="utf-8") as stream:
            stream.write("mutated\n")
        return [FinalGame("NBA", "example", 2025,
                          date(2025, 10, 2), "H", "A", "H")]
    monkeypatch.setattr(trainer, "load_final_games_csv", mutate_during_parse)
    option = ("--forward-holdout" if evidence == "forward_holdout"
              else "--independent-history")
    monkeypatch.setattr(
        sys, "argv", [
            "train_league_models.py", "--sport", "NBA",
            "--output-dir", str(output), option, str(path),
        ],
    )
    match = ("forward holdout changed during parsing" if evidence == "forward_holdout"
             else "independent history changed during parsing")
    with pytest.raises(ValueError, match=match):
        trainer.main()
    assert not output.exists()
