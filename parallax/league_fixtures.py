"""Validated source fixture identities shared by NBA/NHL scan modes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ShadowFixture:
    """Compatibility name retained for the pre-existing shadow test seam."""

    sport: str
    game_id: str
    season: int
    kickoff: datetime
    home_team: str
    away_team: str
    venue_home_name: str = ""
    venue_away_name: str = ""

    def __post_init__(self) -> None:
        if (
            self.sport not in {"NBA", "NHL"}
            or not self.game_id
            or not self.home_team
            or not self.away_team
            or self.home_team == self.away_team
            or self.kickoff.tzinfo is None
            or self.kickoff.utcoffset() is None
            or bool(self.venue_home_name) != bool(self.venue_away_name)
            or (
                self.venue_home_name
                and " ".join(self.venue_home_name.split()).casefold()
                == " ".join(self.venue_away_name.split()).casefold()
            )
        ):
            raise ValueError("Invalid or ambiguous league fixture")


def nba_fixture_from_balldontlie(
    game: Mapping[str, object],
    *,
    venue_home_name: str,
    venue_away_name: str,
    regular_season_query_verified: bool = False,
) -> ShadowFixture:
    if not regular_season_query_verified or not isinstance(game, Mapping):
        raise ValueError("Verified NBA regular-season query required")
    game_id, season = game.get("id"), game.get("season")
    if (
        type(game_id) is not int
        or game_id <= 0
        or type(season) is not int
        or not 2000 <= season <= 2200
        or game.get("postseason") is not False
        or game.get("postponed") is True
        or str(game.get("status_state") or "").lower() != "scheduled"
        or str(game.get("season_type") or "regular").lower() != "regular"
        or str(game.get("ist_stage") or "").lower() == "championship"
    ):
        raise ValueError("NBA game is not an eligible scheduled regular fixture")
    home, away = game.get("home_team"), game.get("visitor_team")
    if not isinstance(home, Mapping) or not isinstance(away, Mapping):
        raise ValueError("NBA fixture requires both source team records")  # noqa: TRY004
    h, a = (
        str(home.get("abbreviation") or "").strip().upper(),
        str(away.get("abbreviation") or "").strip().upper(),
    )
    if not re.fullmatch(r"[A-Z]{2,4}", h) or not re.fullmatch(r"[A-Z]{2,4}", a):
        raise ValueError("NBA fixture has invalid team codes")
    raw_start, raw_day = game.get("datetime"), game.get("date")
    if not isinstance(raw_start, str) or not isinstance(raw_day, str):
        raise ValueError("NBA fixture missing timezone-aware start and date")  # noqa: TRY004
    try:
        start = datetime.fromisoformat(raw_start)
        scheduled_day = date.fromisoformat(raw_day)
    except ValueError as exc:
        raise ValueError("NBA fixture date/start invalid") from exc
    if (
        start.tzinfo is None
        or start.utcoffset() is None
        or start.astimezone(_ET).date() != scheduled_day
    ):
        raise ValueError("NBA local game date contradicts UTC tipoff")
    if not venue_home_name.strip() or not venue_away_name.strip():
        raise ValueError("Explicit venue team aliases required")
    return ShadowFixture(
        "NBA", f"bdl:{game_id}", season, start, h, a, venue_home_name, venue_away_name
    )
