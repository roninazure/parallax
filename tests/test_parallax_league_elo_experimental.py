"""Synthetic offline tests only. No live provider access or real results."""
from datetime import date

import pytest

from parallax.league_elo_experimental import (
    MODEL_STATUS, FinalGame, holdout_metrics, load_final_games_csv,
    predict_future, replay,
)


def game(sport, ident, date_str, home, away, winner, season=2025):
    return FinalGame(sport, ident, season, date.fromisoformat(date_str),
                     home, away, winner)


def test_both_sports_keep_separate_ratings():
    rows = [
        game("NBA", "nba1", "2025-10-20", "A", "B", "A"),
        game("NHL", "nhl1", "2025-10-20", "A", "B", "B"),
        game("NBA", "nba2", "2025-10-21", "A", "B", "A"),
        game("NHL", "nhl2", "2025-10-21", "A", "B", "B"),
    ]
    forecasts = {p.game_id: p for p in replay(rows)}
    assert forecasts["nba2"].home_win_probability > forecasts["nba1"].home_win_probability
    assert forecasts["nhl2"].home_win_probability < forecasts["nhl1"].home_win_probability
    assert all(p.model_status == MODEL_STATUS for p in forecasts.values())


def test_same_day_future_results_cannot_leak_into_other_forecasts():
    first = game("NBA", "g1", "2025-10-20", "A", "B", "A")
    second = game("NBA", "g2", "2025-10-20", "A", "B", "B")
    forecasts = {p.game_id: p for p in replay([second, first])}
    assert forecasts["g1"].home_win_probability == forecasts["g2"].home_win_probability


def test_future_ignores_target_date_results():
    past = game("NHL", "g1", "2025-10-20", "BOS", "MTL", "BOS")
    future_day = game("NHL", "g2", "2025-10-21", "BOS", "MTL", "MTL")
    args = dict(sport="NHL", game_date=date(2025, 10, 21),
                home_team="BOS", away_team="MTL")
    p1 = predict_future([past], **args)
    p2 = predict_future([future_day, past], **args)
    assert p1.home_win_probability == p2.home_win_probability
    assert p1.home_won is None


def test_holdout_evaluation_is_descriptive_not_certification():
    forecasts = replay([
        game("NBA", "n1", "2025-10-20", "A", "B", "A"),
        game("NBA", "n2", "2025-10-21", "B", "A", "A"),
    ])
    m = holdout_metrics(forecasts, sport="NBA", season=2025)
    assert m.n_games == 2
    assert 0 <= m.brier <= 1
    assert 0 <= m.ece_10_bins <= 1
    assert m.model_status == "EXPERIMENTAL_NOT_CERTIFIED"
    assert holdout_metrics(forecasts, sport="NHL", season=2025).n_games == 0


def test_duplicate_and_bad_full_game_winner_fail_closed():
    g = game("NHL", "g1", "2025-10-20", "A", "B", "A")
    with pytest.raises(ValueError, match="Duplicate"):
        replay([g, g])
    with pytest.raises(ValueError, match="winner"):
        game("NHL", "g2", "2025-10-20", "A", "B", "TIE")
    with pytest.raises(ValueError, match="Unsupported sport"):
        game("MLB", "g3", "2025-10-20", "A", "B", "A")


def test_csv_accepts_only_canonical_verified_results(tmp_path):
    path = tmp_path / "finals.csv"
    path.write_text("sport,game_id,season,game_date,home_team,away_team,winner_team\n"
                    "nba,game1,2025,2025-10-20,BOS,NYK,BOS\n")
    rows = load_final_games_csv(path)
    assert rows[0].winner_team == "BOS"
    assert rows[0].sport == "NBA"
