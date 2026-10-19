"""Synthetic offline examples, not licensed source records."""
from dataclasses import replace
from pathlib import Path

import pytest
from parallax.league_snapshot_adapters import (
    nba_from_balldontlie, nhl_from_club_schedule, merge_deduplicated,
    export_canonical_csv,
)
from parallax.league_elo_experimental import load_final_games_csv


def nba_game(**changes):
    row = dict(id=100, season=2025, date="2025-10-20", status_state="final",
               postseason=False, home_team={"abbreviation":"BOS"},
               visitor_team={"abbreviation":"NYK"}, home_team_score=101,
               visitor_team_score=97)
    row.update(changes)
    return row


def nhl_game(**changes):
    row = dict(id=2025020001, season=20252026, gameType=2, gameState="OFF",
               gameDate="2025-10-07", homeTeam={"abbrev":"BOS", "score":4},
               awayTeam={"abbrev":"MTL", "score":3})
    row.update(changes)
    return row


def test_nba_requires_verified_regular_season_query():
    with pytest.raises(ValueError, match="query not verified"):
        nba_from_balldontlie({"data": [nba_game()]})


def test_nba_only_final_regular_season_and_home_away_identity():
    rows = nba_from_balldontlie({"data": [nba_game(), nba_game(id=101,status_state="in_progress"),
        nba_game(id=102,postseason=True), nba_game(id=103,season_type="playoffs")]},
        regular_season_query_verified=True)
    assert [(g.sport, g.home_team, g.away_team, g.winner_team) for g in rows] == [
        ("NBA", "BOS", "NYK", "BOS")]


def test_nhl_regular_final_includes_extra_time_results():
    rows = nhl_from_club_schedule({"games": [nhl_game(),
        nhl_game(id=2025020002,gameType=3), nhl_game(id=2025020003,gameState="LIVE")]})
    assert len(rows) == 1
    assert rows[0].winner_team == "BOS"
    assert rows[0].season == 2025


def test_final_tie_and_missing_score_fail_closed():
    with pytest.raises(ValueError, match="cannot be tied"):
        nhl_from_club_schedule({"games": [nhl_game(homeTeam={"abbrev":"BOS", "score":3})]})
    with pytest.raises(ValueError, match="nonnegative integers"):
        nba_from_balldontlie({"data": [nba_game(home_team_score=None)]}, regular_season_query_verified=True)


def test_conflicting_duplicate_is_rejected_and_identical_deduplicated():
    rows = nhl_from_club_schedule({"games": [nhl_game()]})
    assert len(merge_deduplicated(rows, rows)) == 1
    with pytest.raises(ValueError, match="Conflicting fixture"):
        merge_deduplicated(rows, [replace(rows[0], winner_team="MTL")])


def test_offline_csv_round_trip(tmp_path: Path):
    games = merge_deduplicated(
        nba_from_balldontlie({"data": [nba_game()]}, regular_season_query_verified=True),
        nhl_from_club_schedule({"games": [nhl_game()]})
    )
    path = tmp_path / "games.csv"
    export_canonical_csv(path, games)
    assert load_final_games_csv(path) == games
