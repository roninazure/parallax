"""Synthetic-only NBA calibration candidate tests; never use inspected holdout."""
import csv
import json
from datetime import date, timedelta

import pytest

from parallax.league_elo_experimental import FinalGame
from parallax.nba_elo_calibration_candidate import evaluate_nba_elo_candidate
from scripts.nba_elo_calibration_candidate import main

FIRST = date(2024, 10, 1)
TRAIN_END = FIRST + timedelta(days=49)
CAL_END = FIRST + timedelta(days=79)


def games():
    train, cal = [], []
    for day in range(80):
        for pair in range(8):
            home, away = f"H{pair}", f"A{pair}"
            # Deterministic varied outcomes; no actual provider data.
            home_won = (day * 7 + pair * 3) % 13 < (9 if pair % 2 else 6)
            row = FinalGame(
                "NBA", f"g-{day}-{pair}", 2024,
                FIRST + timedelta(days=day), home, away,
                home if home_won else away,
            )
            (train if day < 50 else cal).append(row)
    return train, cal


def evaluate(train=None, cal=None):
    a, b = games()
    return evaluate_nba_elo_candidate(
        a if train is None else train, b if cal is None else cal,
        train_end=TRAIN_END, calibration_end=CAL_END,
        home_advantage=40.0, k=30.0,
    )


def test_candidate_is_deterministic_date_isolated_and_uncertified():
    result = evaluate()
    assert result == evaluate()
    assert result["status"] == "RESEARCH_ONLY_UNCERTIFIED"
    assert result["certified"] is False
    assert result["forward_holdout_evaluated"] is False
    assert result["fit_end"] < result["validation_start"]
    assert result["train_end"] < result["validation_start"]
    assert result["validation_start"] <= result["calibration_end"]
    assert result["fit_games"] + result["validation_games"] == 240
    assert result["validation_games"] > 0
    assert "elo_raw" in result["candidate_validation"]
    assert "platt_all" in result["candidate_validation"]
    assert result["selected_family"] in result["candidate_validation"]
    assert result["candidate_validation"]["elo_raw"]["n_games"] == result["validation_games"]
    assert result["baseline_50_50_validation"]["brier"] == 0.25


def test_same_day_order_does_not_change_candidate():
    train, cal = games()
    assert evaluate(train[::-1], cal[::-1]) == evaluate(train, cal)


def test_rejects_later_outcomes_and_cross_boundary_rows():
    train, cal = games()
    future = FinalGame("NBA", "future", 2024, CAL_END + timedelta(days=1),
                       "H1", "A1", "H1")
    with pytest.raises(ValueError, match="out-of-window"):
        evaluate(train, cal + [future])
    with pytest.raises(ValueError, match="post-train-cutoff"):
        evaluate(train + [cal[0]], cal)
    with pytest.raises(ValueError, match="overlap"):
        evaluate_nba_elo_candidate(
            train, cal, train_end=CAL_END, calibration_end=CAL_END,
            home_advantage=40.0, k=30.0,
        )


def test_rejects_missing_calibration_class_and_invalid_parameters():
    train, cal = games()
    all_home = [
        FinalGame(g.sport, g.game_id, g.season, g.game_date,
                  g.home_team, g.away_team, g.home_team)
        for g in cal
    ]
    with pytest.raises(ValueError, match="both outcomes"):
        evaluate(train, all_home)
    with pytest.raises(ValueError, match="positive K"):
        evaluate_nba_elo_candidate(
            train, cal, train_end=TRAIN_END, calibration_end=CAL_END,
            home_advantage=40.0, k=0.0,
        )


def _write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["sport", "game_id", "season", "game_date",
                         "home_team", "away_team", "winner_team"])
        for g in rows:
            writer.writerow([g.sport, g.game_id, g.season, g.game_date.isoformat(),
                             g.home_team, g.away_team, g.winner_team])


def test_read_only_cli_uses_separate_files_and_reports_hashes(tmp_path, capsys):
    train, cal = games()
    a, b = tmp_path / "train.csv", tmp_path / "cal.csv"
    _write_csv(a, train)
    _write_csv(b, cal)
    args = ["--train", str(a), "--calibration", str(b),
            "--train-end", TRAIN_END.isoformat(),
            "--calibration-end", CAL_END.isoformat(),
            "--home-advantage", "40", "--k", "30"]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source_authorization_verified"] is False
    assert len(result["input_sha256"]["train"]) == 64
    assert result["forward_holdout_evaluated"] is False
    with pytest.raises(SystemExit):
        main(["--train", str(a), "--calibration", str(a), *args[4:]])
