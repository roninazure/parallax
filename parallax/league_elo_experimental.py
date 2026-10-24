"""Offline-only NBA/NHL full-game winner baseline.

EXPERIMENTAL / NOT CERTIFIED.  This module has no venue/network imports,
no feed publication, no scheduler registration, and no BUY authority.

Model input comes from a locally verified final-results snapshot.  Games
on the same official (ET) date are evaluated before any of that day's final
results update ratings, avoiding same-day future-result leakage.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

SPORT_PARAMS: dict[str, tuple[float, float]] = {
    # (home Elo adjustment, rating update K). Experimental starting points,
    # NOT measured or calibrated parameters.
    "NBA": (60.0, 20.0),
    "NHL": (35.0, 20.0),
}
BASE_RATING = 1500.0
MODEL_STATUS = "EXPERIMENTAL_NOT_CERTIFIED"


@dataclass(frozen=True)
class FinalGame:
    sport: str
    game_id: str
    season: int
    game_date: date  # Official fixture date, America/New_York calendar.
    home_team: str
    away_team: str
    winner_team: str  # FINAL full-game winner; includes OT/shootout in NHL.

    def __post_init__(self) -> None:
        if self.sport not in SPORT_PARAMS:
            raise ValueError(f"Unsupported sport: {self.sport!r}")
        if not self.game_id.strip() or not self.home_team.strip() or not self.away_team.strip():
            raise ValueError("Game ID and both team IDs are required")
        if self.home_team == self.away_team:
            raise ValueError("A team cannot play itself")
        if self.winner_team not in (self.home_team, self.away_team):
            raise ValueError("Full-game winner must equal one of the participating teams")
        if self.season < 2000 or self.season > 2200:
            raise ValueError("Season must be the starting year, e.g. 2026")


@dataclass(frozen=True)
class Forecast:
    sport: str
    game_id: str
    season: int
    game_date: date
    home_team: str
    away_team: str
    home_win_probability: float
    home_won: bool | None
    model_status: str = MODEL_STATUS


@dataclass(frozen=True)
class HoldoutMetrics:
    sport: str
    holdout_season: int
    n_games: int
    brier: float | None
    log_loss: float | None
    ece_10_bins: float | None
    model_status: str = MODEL_STATUS


def _home_probability(sport: str, home_rating: float, away_rating: float) -> float:
    home_advantage, _ = SPORT_PARAMS[sport]
    delta = (home_rating + home_advantage - away_rating) / 400.0
    return 1.0 / (1.0 + 10.0 ** (-delta))


def _prepare(games: Iterable[FinalGame]) -> list[FinalGame]:
    rows = list(games)
    keys: set[tuple[str, str]] = set()
    for game in rows:
        key = (game.sport, game.game_id)
        if key in keys:
            raise ValueError(f"Duplicate final result: {key}")
        keys.add(key)
    return sorted(rows, key=lambda g: (g.game_date, g.sport, g.game_id))


def replay(games: Iterable[FinalGame]) -> list[Forecast]:
    """Chronological, pre-date forecasts from results of prior dates only.

    The current date's results are NEVER available to any prediction from
    that date.  Separate independent NBA/NHL Elo state is maintained.
    """
    rows = _prepare(games)
    ratings: dict[tuple[str, str], float] = {}
    predictions: list[Forecast] = []
    i = 0
    while i < len(rows):
        j = i + 1
        while j < len(rows) and rows[j].game_date == rows[i].game_date:
            j += 1
        day = rows[i:j]
        day_predictions: list[tuple[FinalGame, float]] = []
        for game in day:
            h = ratings.get((game.sport, game.home_team), BASE_RATING)
            a = ratings.get((game.sport, game.away_team), BASE_RATING)
            p = _home_probability(game.sport, h, a)
            day_predictions.append((game, p))
            predictions.append(Forecast(
                sport=game.sport, game_id=game.game_id,
                season=game.season, game_date=game.game_date,
                home_team=game.home_team, away_team=game.away_team,
                home_win_probability=p,
                home_won=game.winner_team == game.home_team,
            ))
        # Freeze same-day pregame predictions above. Update ratings only now.
        # For multiple fixtures involving a team on the same date, accumulate
        # results from the frozen pre-date state rather than update mid-day.
        changes: dict[tuple[str, str], float] = {}
        for game, p in day_predictions:
            result = float(game.winner_team == game.home_team)
            _, k = SPORT_PARAMS[game.sport]
            delta = k * (result - p)
            home_key = (game.sport, game.home_team)
            away_key = (game.sport, game.away_team)
            changes[home_key] = changes.get(home_key, 0.0) + delta
            changes[away_key] = changes.get(away_key, 0.0) - delta
        for key, delta in changes.items():
            ratings[key] = ratings.get(key, BASE_RATING) + delta
        i = j
    return predictions


def predict_future(
    history: Iterable[FinalGame], *, sport: str, game_date: date,
    home_team: str, away_team: str, game_id: str = "future",
    season: int = 2026,
) -> Forecast:
    """Project a fixture from prior-day results; no current-day leakage."""
    if sport not in SPORT_PARAMS or not home_team or not away_team or home_team == away_team:
        raise ValueError("Unsupported sport or invalid team identity")
    past = [g for g in _prepare(history) if g.game_date < game_date]
    # Reuse identical date-bucket semantics for the model state by adding an
    # artificial no-op target only after replaying prior dates.
    ratings: dict[tuple[str, str], float] = {}
    for p in replay(past):
        assert p.home_won is not None
        _, k = SPORT_PARAMS[p.sport]
        delta = k * (float(p.home_won) - p.home_win_probability)
        hk, ak = (p.sport, p.home_team), (p.sport, p.away_team)
        ratings[hk] = ratings.get(hk, BASE_RATING) + delta
        ratings[ak] = ratings.get(ak, BASE_RATING) - delta
    prob = _home_probability(
        sport, ratings.get((sport, home_team), BASE_RATING),
        ratings.get((sport, away_team), BASE_RATING)
    )
    return Forecast(sport, game_id, season, game_date, home_team,
                    away_team, prob, None)


def holdout_metrics(predictions: Iterable[Forecast], *, sport: str,
                    season: int) -> HoldoutMetrics:
    chosen = [p for p in predictions if p.sport == sport and
              p.season == season and p.home_won is not None]
    if not chosen:
        return HoldoutMetrics(sport, season, 0, None, None, None)
    n = len(chosen)
    brier = sum((p.home_win_probability - float(p.home_won)) ** 2
                for p in chosen) / n
    logloss = -sum(
        (1.0 if p.home_won else 0.0) * math.log(p.home_win_probability)
        + (0.0 if p.home_won else 1.0) * math.log1p(-p.home_win_probability)
        for p in chosen
    ) / n
    bins: list[list[Forecast]] = [[] for _ in range(10)]
    for p in chosen:
        bins[min(9, int(10 * p.home_win_probability))].append(p)
    ece = sum(
        len(bucket) / n * abs(
            sum(float(p.home_won) for p in bucket) / len(bucket)
            - sum(p.home_win_probability for p in bucket) / len(bucket)
        ) for bucket in bins if bucket
    )
    return HoldoutMetrics(sport, season, n, brier, logloss, ece)


def load_final_games_csv(path: str | Path) -> list[FinalGame]:
    """Read a local canonical final-results CSV. No external requests.

    Required columns: sport, game_id, season, game_date, home_team,
    away_team, winner_team.  Only verified final REGULAR-SEASON winners
    belong in this snapshot; no sportsbook prices or market results.
    """
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        rows = [FinalGame(
            sport=row["sport"].strip().upper(),
            game_id=row["game_id"].strip(),
            season=int(row["season"]),
            game_date=date.fromisoformat(row["game_date"]),
            home_team=row["home_team"].strip().upper(),
            away_team=row["away_team"].strip().upper(),
            winner_team=row["winner_team"].strip().upper(),
        ) for row in csv.DictReader(stream)]
    return _prepare(rows)
