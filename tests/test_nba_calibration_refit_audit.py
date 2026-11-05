"""Refit comparison is exact-data-bound and always noncertifying."""
import csv
import hashlib
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from parallax.league_elo_experimental import load_final_games_csv
from parallax.league_model import train_artifact
from scripts.nba_calibration_refit_audit import compare


FIELDS = ("sport", "game_id", "season", "game_date", "home_team",
          "away_team", "winner_team")


def sample(tmp_path):
    history = tmp_path / "nba.csv"
    with history.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=FIELDS)
        writer.writeheader()
        for day in range(60):
            when = (date(2024, 10, 1) + timedelta(days=day)).isoformat()
            for game in range(4):
                writer.writerow({
                    "sport": "NBA", "game_id": f"nba-{day}-{game}",
                    "season": 2024, "game_date": when,
                    "home_team": f"H{game}", "away_team": f"A{game}",
                    "winner_team": f"H{game}" if (day + game) % 3 else f"A{game}",
                })
    digest = hashlib.sha256(history.read_bytes()).hexdigest()
    baseline = train_artifact(load_final_games_csv(history), sport="NBA",
                              source_sha256=digest,
                              generated_at=datetime(2025, 1, 1, tzinfo=UTC))
    frozen = tmp_path / "frozen.json"
    frozen.write_text(json.dumps(baseline), encoding="utf-8")
    return history, frozen


def test_exact_historical_refit_is_repeatable_and_unpublishable(tmp_path):
    history, frozen = sample(tmp_path)
    first = compare(history, frozen)
    second = compare(history, frozen)
    assert first == second
    assert first["source_games"] == 240
    assert first["holdout_games"] > 0
    assert first["model_certified"] is False
    assert first["buy_authorized"] is False
    assert first["publication_eligible"] is False
    assert first["holdout_status"] == "PREVIOUSLY_INSPECTED_NOT_INDEPENDENT"
    assert set(first["metric_changes"]) == {"brier", "log_loss", "ece_10_bins"}
    assert first["research_candidate_sha256_after"] == first["model_artifact_sha256_before"]


def test_changed_history_is_rejected_before_refit(tmp_path):
    history, frozen = sample(tmp_path)
    with history.open("a", encoding="utf-8") as stream:
        stream.write("NBA,unapproved,2024,2024-11-30,H0,A0,H0\n")
    with pytest.raises(ValueError, match="source digest mismatch"):
        compare(history, frozen)


def test_changed_or_impostor_baseline_is_rejected(tmp_path):
    history, frozen = sample(tmp_path)
    artifact = json.loads(frozen.read_text())
    artifact["holdout_metrics"]["ece_10_bins"] = 0
    frozen.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        compare(history, frozen)


def test_non_nba_baseline_is_rejected(tmp_path):
    history, frozen = sample(tmp_path)
    artifact = json.loads(frozen.read_text())
    artifact["sport"] = "NHL"
    frozen.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(ValueError, match="NBA baseline"):
        compare(history, frozen)
