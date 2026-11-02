"""Offline deterministic tests; no external services, runtime files, or money movement."""
import csv
import io

import pytest

from scripts.nfl_market_benchmark import _market_home_probability, benchmark

COLUMNS = ("game_id", "season", "game_type", "gameday", "gametime",
           "home_team", "away_team", "home_score", "away_score",
           "home_moneyline", "away_moneyline")


def sample(game_id, day, home_score, away_score, **kw):
    return dict(game_id=game_id, season="2025", game_type="REG", gameday=day,
                gametime="13:00", home_team="KC", away_team="LV",
                home_score=home_score, away_score=away_score,
                home_moneyline="-200", away_moneyline="+180", **kw)


def source(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def test_market_de_vig_rejects_missing_and_malformed_odds():
    p = _market_home_probability({"home_moneyline": "-200", "away_moneyline": "+180"})
    assert .60 < p < .70
    assert _market_home_probability({"home_moneyline": "", "away_moneyline": "+180"}) is None
    assert _market_home_probability({"home_moneyline": "-200", "away_moneyline": "inf"}) is None


def test_only_real_binary_and_matched_games_enter_comparison():
    data = [
        sample("a", "2025-09-07", "21", "14"),
        sample("tied", "2025-09-14", "20", "20"),
        {**sample("no-odds", "2025-09-21", "7", "21"), "home_moneyline": ""},
    ]
    result = benchmark(source(data), min_paired=2)
    assert result["completed_binary_games"] == 2
    assert result["paired_market_games"] == 1
    assert result["unpaired_games"] == 1
    assert result["status"] == "INSUFFICIENT_PAIRED_ODDS"


def test_target_result_cannot_change_its_own_model_forecast():
    one = sample("one", "2025-09-07", "21", "14")
    two = sample("two", "2025-09-14", "14", "17")
    a = benchmark(source([one, two]), min_paired=1)
    b = benchmark(source([one, {**two, "home_score": "42", "away_score": "0"}]), min_paired=1)
    assert a["paired_comparison"]["elo"]["mean_probability"] == b["paired_comparison"]["elo"]["mean_probability"]
    assert a["source_sha256"] != b["source_sha256"]


def test_order_of_same_kickoff_games_does_not_change_forecasts():
    first = sample("one", "2025-09-07", "21", "14")
    second = {**sample("two", "2025-09-07", "0", "40"), "home_team": "KC", "away_team": "DEN"}
    a = benchmark(source([first, second]), min_paired=1)
    b = benchmark(source([second, first]), min_paired=1)
    assert a["paired_comparison"] == b["paired_comparison"]


def test_missing_odds_columns_fail_loudly():
    with pytest.raises(ValueError, match="missing required historical columns"):
        benchmark("game_id,season\na,2025\n")
