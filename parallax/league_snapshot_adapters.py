"""Strict, offline-only adapters for locally obtained NBA/NHL game-result JSON.

Does not fetch data, determine licensing rights, publish plays, or issue alerts.
These adapters accept candidate source snapshots for *research*, not certified BUYs.
Unrecognized/unfinished game rows are skipped; malformed finalized rows fail closed.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from .league_elo_experimental import FinalGame, _prepare

FIELDS = ("sport", "game_id", "season", "game_date", "home_team", "away_team", "winner_team")


def _game_date(value: object) -> date:
    return date.fromisoformat(str(value)[:10])


def _score(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Final scores must be nonnegative integers")
    return value


def _winner(home_team: str, away_team: str, home_score: object, away_score: object) -> str:
    home, away = _score(home_score), _score(away_score)
    if home == away:
        raise ValueError("Final full-game score cannot be tied")
    return home_team if home > away else away_team


def _rows(payload: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get(key), list):
        raise ValueError(f"Expected JSON object containing {key!r} array")
    results: list[Mapping[str, Any]] = []
    for row in payload[key]:
        if not isinstance(row, Mapping):
            raise ValueError("Malformed game row")
        results.append(row)
    return results


def nba_from_balldontlie(payload: Mapping[str, Any], *, regular_season_query_verified: bool = False) -> list[FinalGame]:
    """Parse saved GET /v1/games JSON. Must have requested regular-season only.

    BDL's older responses do not always include a per-game season_type field.
    Therefore the caller must confirm the *saved request* included
    season_type=regular; final/properly scored records alone are insufficient.
    """
    if not regular_season_query_verified:
        raise ValueError("Regular-season source query not verified")
    games = []
    for row in _rows(payload, "data"):
        season_type = str(row.get("season_type") or "regular").lower()
        # NBA Cup group, quarterfinal and semifinal games ARE regular-season
        # games.  Only the Cup Championship is outside regular-season records.
        # The originating request must have season_type=regular; never ingest
        # separately fetched season_type=ist snapshots as regular-season games.
        cup_stage = str(row.get("ist_stage") or "").strip().lower()
        if (season_type != "regular" or row.get("postseason") is True
                or cup_stage == "championship"):
            continue
        if str(row.get("status_state") or row.get("status") or "").lower() not in {"final", "final/ot"}:
            continue
        home, away = row.get("home_team"), row.get("visitor_team")
        if not isinstance(home, Mapping) or not isinstance(away, Mapping):
            raise ValueError("Final NBA game missing team records")
        h, a = str(home.get("abbreviation") or "").upper(), str(away.get("abbreviation") or "").upper()
        games.append(FinalGame(
            sport="NBA", game_id=f"bdl:{row['id']}", season=int(row["season"]),
            game_date=_game_date(row["date"]), home_team=h, away_team=a,
            winner_team=_winner(h, a, row.get("home_team_score"), row.get("visitor_team_score")),
        ))
    return _prepare(games)


def nhl_from_club_schedule(payload: Mapping[str, Any]) -> list[FinalGame]:
    """Parse a locally saved NHL club-season schedule. Do NOT auto-fetch it.

    NHL data/content may require explicit commercial license. Game type=2 is
    regular season. OFF is the completed official game state; finished games
    include overtime/shootout in the final score.
    """
    games = []
    for row in _rows(payload, "games"):
        if row.get("gameType") != 2:
            continue
        if str(row.get("gameState") or "").upper() != "OFF":
            continue
        home, away = row.get("homeTeam"), row.get("awayTeam")
        if not isinstance(home, Mapping) or not isinstance(away, Mapping):
            raise ValueError("Final NHL game missing team records")
        h, a = str(home.get("abbrev") or "").upper(), str(away.get("abbrev") or "").upper()
        games.append(FinalGame(
            sport="NHL", game_id=f"nhl:{row['id']}",
            season=int(str(row["season"])[:4]), game_date=_game_date(row["gameDate"]),
            home_team=h, away_team=a,
            winner_team=_winner(h, a, home.get("score"), away.get("score")),
        ))
    return _prepare(games)


def merge_deduplicated(*sources: list[FinalGame]) -> list[FinalGame]:
    """Deduplicate NHL club schedules; conflicting mirrored rows fail closed."""
    by_key: dict[tuple[str, str], FinalGame] = {}
    for source in sources:
        for game in source:
            key = game.sport, game.game_id
            if key in by_key and by_key[key] != game:
                raise ValueError(f"Conflicting fixture data for {key}")
            by_key[key] = game
    return _prepare(by_key.values())


def export_canonical_csv(path: str | Path, games: list[FinalGame]) -> None:
    """Export to canonical baseline schema, offline, output path chosen by caller."""
    rows = _prepare(games)
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for game in rows:
            writer.writerow({
                "sport": game.sport, "game_id": game.game_id,
                "season": game.season, "game_date": game.game_date.isoformat(),
                "home_team": game.home_team, "away_team": game.away_team,
                "winner_team": game.winner_team,
            })
