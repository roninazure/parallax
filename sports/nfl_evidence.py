"""Read-only NFL sportsbook evidence adapter; never authorizes a BUY.

Consumes the existing provider's in-memory events, without making API calls.
An event is usable only when its participants and kickoff match exactly one
official fixture, and at least two independent fresh books agree on sides.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from parallax.nfl import NFLGame, _team_key

from .fair_value import consensus_two_way_moneyline
from .odds_provider import SportsEvent


@dataclass(frozen=True)
class NFLMarketEvidence:
    game_id: str
    observed_at: str
    kickoff: str
    home_probability: float
    away_probability: float
    independent_books: tuple[str, ...]
    bookmaker_count: int


def _utc(value: str) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def match_nfl_evidence(
    event: SportsEvent,
    fixtures: list[NFLGame],
    *,
    observed_at: datetime,
    max_kickoff_delta_seconds: int = 300,
    max_quote_age_seconds: float = 300,
    min_books: int = 2,
) -> NFLMarketEvidence | None:
    """Return validated independent consensus, or fail closed to None.

    This does not validate prediction quality or permit customer publication.
    """
    if observed_at.tzinfo is None:
        return None
    if event.league.strip().casefold() != "nfl":
        return None
    observed = observed_at.astimezone(UTC)
    start = _utc(event.start_time)
    if start is None or start <= observed:
        return None
    home, away = _team_key(event.home_team), _team_key(event.away_team)
    if not home or not away or home == away:
        return None
    matching = [
        game for game in fixtures
        if _team_key(game.home_team) == home
        and _team_key(game.away_team) == away
        and (kickoff := _utc(game.kickoff)) is not None
        and abs((kickoff - start).total_seconds()) <= max_kickoff_delta_seconds
        and kickoff > observed
    ]
    if len(matching) != 1:
        return None
    fair = consensus_two_way_moneyline(
        list(event.moneylines), max_age_seconds=max_quote_age_seconds,
        min_books=min_books,
    )
    if fair is None:
        return None
    game = matching[0]
    return NFLMarketEvidence(
        game_id=game.game_id,
        observed_at=observed.isoformat(),
        kickoff=game.kickoff,
        home_probability=fair.outcome_a_probability,
        away_probability=fair.outcome_b_probability,
        independent_books=fair.bookmakers_used,
        bookmaker_count=fair.sample_size,
    )
