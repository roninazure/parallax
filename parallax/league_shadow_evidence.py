"""Isolated NBA/NHL -> existing PARALLAX Evidence/qualify seam.

RESEARCH / SHADOW ONLY. No provider calls, certification, scheduler, public
feed, alerts, or order operations. Unverified venue shapes fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import re
from typing import Mapping
from zoneinfo import ZoneInfo

from .engine import qualify
from .league_elo_experimental import Forecast, MODEL_STATUS
from .models import Action, Evidence, NormalizedMarket, ParallaxPlay, Side, Venue
from .normalization import rules_digest

_ET = ZoneInfo("America/New_York")
_MODEL_VERSION = "nba-nhl-elo-experimental-v0"
_ALLOWED = {"NBA": "KXNBAGAME", "NHL": "KXNHLGAME"}
_KALSHI_WINNER_RULE = re.compile(
    r"If (?P<winner>.+?) wins the (?P<first>.+?) vs (?P<second>.+?) "
    r"(?P<league>Pro Basketball|NHL) game originally scheduled for "
    r"(?P<date>[A-Z][a-z]{2} [0-9]{1,2}, [0-9]{4}), "
    r"then the market resolves to Yes\.", re.ASCII
)

_PMUS_NBA_RULE = re.compile(
    r"^This market will settle to the winner of "
    r"(?P<away_code>[A-Z]{2,4}) (?P<away_name>[A-Za-z ]+?) vs "
    r"(?P<home_code>[A-Z]{2,4}) (?P<home_name>[A-Za-z ]+?) "
    r"professional basketball game originally scheduled for "
    r"(?P<date>[A-Z][a-z]{2} [0-9]{1,2}, [0-9]{4}) at "
    r"(?P<time>[0-9]{1,2}:[0-9]{2}[AP]M) ET\\. "
    r"Overtime is included if played\\.", re.ASCII
)
# Verified NBA venue alias in the 2026-10-26 Golden State @ Denver contract.
# Do not extrapolate unverified NBA aliases or NHL settlement semantics.
_PMUS_NBA_CODES = {"GSW": "GS"}


def _pmus_nba_code(canonical: str) -> str:
    return _PMUS_NBA_CODES.get(canonical, canonical)




@dataclass(frozen=True)
class ShadowFixture:
    sport: str
    game_id: str
    season: int
    kickoff: datetime  # Authoritative, timezone-aware actual tipoff / puck drop
    home_team: str      # Canonical abbreviation, not display name
    away_team: str
    venue_home_name: str = ""  # Explicit short team name from independently verified fixture alias
    venue_away_name: str = ""

    def __post_init__(self) -> None:
        if (self.sport not in _ALLOWED or not self.game_id
                or not self.home_team or not self.away_team
                or self.home_team == self.away_team
                or self.kickoff.tzinfo is None
                or self.kickoff.utcoffset() is None
                or bool(self.venue_home_name) != bool(self.venue_away_name)
                or (self.venue_home_name and _name(self.venue_home_name) == _name(self.venue_away_name))):
            raise ValueError("Invalid or ambiguous shadow fixture")


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(UTC) if dt.tzinfo and dt.utcoffset() is not None else None
    except ValueError:
        return None


def _team(value: object) -> str:
    return str(value).strip().upper() if isinstance(value, str) else ""


def _name(value: object) -> str:
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""


def nba_fixture_from_balldontlie(
    game: Mapping[str, object], *,
    venue_home_name: str, venue_away_name: str,
    regular_season_query_verified: bool = False,
) -> ShadowFixture:
    """Offline binding to one source-verified, *scheduled* NBA regular game.

    No fetches, aliases inferred from markets, or fallback to occurrence_datetime.
    Caller must have independently verified the originating regular-season
    query and supplied the verified short venue aliases for both teams.
    """
    if not regular_season_query_verified or not isinstance(game, Mapping):
        raise ValueError("Verified NBA regular-season query required")
    game_id, season = game.get("id"), game.get("season")
    if (type(game_id) is not int or game_id <= 0
            or type(season) is not int or not 2000 <= season <= 2200
            or game.get("postseason") is not False
            or game.get("postponed") is True
            or str(game.get("status_state") or "").lower() != "scheduled"
            or str(game.get("season_type") or "regular").lower() != "regular"
            or str(game.get("ist_stage") or "").lower() == "championship"):
        raise ValueError("NBA game is not an eligible scheduled regular fixture")
    home, away = game.get("home_team"), game.get("visitor_team")
    if not isinstance(home, Mapping) or not isinstance(away, Mapping):
        raise ValueError("NBA fixture requires both source team records")
    h, a = _team(home.get("abbreviation")), _team(away.get("abbreviation"))
    if not re.fullmatch(r"[A-Z]{2,4}", h) or not re.fullmatch(r"[A-Z]{2,4}", a):
        raise ValueError("NBA fixture has invalid team codes")
    start, raw_day = _instant(game.get("datetime")), game.get("date")
    if start is None or not isinstance(raw_day, str):
        raise ValueError("NBA fixture missing timezone-aware start and date")
    try:
        scheduled_day = date.fromisoformat(raw_day)
    except ValueError as exc:
        raise ValueError("NBA fixture date invalid") from exc
    if start.astimezone(_ET).date() != scheduled_day:
        raise ValueError("NBA local game date contradicts UTC tipoff")
    if not _name(venue_home_name) or not _name(venue_away_name):
        raise ValueError("Explicit, separately verified venue team aliases required")
    return ShadowFixture(
        sport="NBA", game_id=f"bdl:{game_id}", season=season,
        kickoff=start, home_team=h, away_team=a,
        venue_home_name=venue_home_name, venue_away_name=venue_away_name,
    )


def _kalshi_yes(market: NormalizedMarket, raw: dict, fixture: ShadowFixture) -> str | None:
    """Verified 2026 KXNBAGAME/KXNHLGAME shape, no invented metadata.

    Match fixture date against ET original scheduled date in ticker AND rule.
    occurrence_datetime is NOT a scheduled game start time.
    """
    if not fixture.venue_home_name or not fixture.venue_away_name:
        return None  # Never guess alias/name mappings from titles
    ticker = raw.get("ticker")
    event = raw.get("event_ticker")
    if not isinstance(ticker, str) or not isinstance(event, str):
        return None
    tokens = ticker.split("-")
    if (len(tokens) != 3 or tokens[0] != _ALLOWED[fixture.sport]
            or event != "-".join(tokens[:2])
            or ticker != market.venue_market_id):
        return None
    local_day = fixture.kickoff.astimezone(_ET).date()
    date_code = local_day.strftime("%y%b%d").upper()
    pair = tokens[1]
    if pair not in {
        date_code + fixture.away_team + fixture.home_team,
        date_code + fixture.home_team + fixture.away_team,
    }:
        return None
    selected = tokens[2]
    names = {fixture.home_team: fixture.venue_home_name,
             fixture.away_team: fixture.venue_away_name}
    if selected not in names:
        return None
    selected_name = names[selected]
    if (_name(raw.get("title")) != _name(selected_name + " wins")
            or _name(raw.get("yes_sub_title")) != _name(selected_name)):
        return None
    primary = raw.get("rules_primary")
    match = _KALSHI_WINNER_RULE.fullmatch(primary) if isinstance(primary, str) else None
    if not match:
        return None
    rule = match.groupdict()
    expected_league = "Pro Basketball" if fixture.sport == "NBA" else "NHL"
    if (rule["league"] != expected_league
            or _name(rule["winner"]) != _name(selected_name)
            or {_name(rule["first"]), _name(rule["second"])}
            != {_name(fixture.venue_home_name), _name(fixture.venue_away_name)}):
        return None
    try:
        original_day = datetime.strptime(rule["date"], "%b %d, %Y").date()
    except ValueError:
        return None
    return selected if original_day == local_day else None


def _selected_yes(market: NormalizedMarket, raw: dict, fixture: ShadowFixture) -> str | None:
    participants = {fixture.home_team, fixture.away_team}
    if market.venue == Venue.KALSHI:
        return _kalshi_yes(market, raw, fixture)
    if market.venue == Venue.POLYMARKET:
        # Only the genuine 2026 PMUS NBA moneyline structure is verified.
        # Validate actual team ordering, identity, scheduled ET kickoff,
        # overtime-inclusive rules and the side chosen by long=True.
        if fixture.sport != "NBA" or raw.get("marketType") != "moneyline":
            return None
        if (raw.get("slug") != market.slug
                or str(raw.get("id")) != market.venue_market_id
                or raw.get("status") != "MARKET_STATUS_OPEN"
                or _instant(raw.get("gameStartTime")) != fixture.kickoff.astimezone(UTC)):
            return None
        home_code = _pmus_nba_code(fixture.home_team)
        away_code = _pmus_nba_code(fixture.away_team)
        local_day = fixture.kickoff.astimezone(_ET).date()
        expected_slug = f"aec-nba-{away_code.lower()}-{home_code.lower()}-{local_day.isoformat()}"
        if market.slug != expected_slug:
            return None

        sides = raw.get("marketSides")
        if not isinstance(sides, list) or len(sides) != 2:
            return None
        ordered: dict[str, dict] = {}
        for side in sides:
            if (not isinstance(side, dict)
                    or type(side.get("long")) is not bool
                    or not isinstance(side.get("team"), dict)):
                return None
            team = side["team"]
            ordering = team.get("ordering")
            if (ordering not in ("home", "away")
                    or ordering in ordered
                    or _team(team.get("league")) != "NBA"):
                return None
            expected_code = home_code if ordering == "home" else away_code
            # Outcome orientation must come from actual 'long' flags, not
            # inferred from the away/home team order.
            if (_team(team.get("abbreviation")) != expected_code
                    or not _name(team.get("name"))
                    or not _name(side.get("description"))
                    or _name(side.get("description")) != _name(team.get("name"))):
                return None
            ordered[ordering] = side
        if set(ordered) != {"home", "away"}:
            return None
        yes = [side for side in sides if side["long"] is True]
        if len(yes) != 1:
            return None
        for side in sides:
            outcome = "YES" if side["long"] else "NO"
            if _name(market.outcomes.get(outcome)) != _name(side["description"]):
                return None

        rules = raw.get("description")
        match = _PMUS_NBA_RULE.match(rules) if isinstance(rules, str) else None
        if not match or rules != market.resolution_rules:
            return None
        identity = match.groupdict()
        if (_team(identity["home_code"]) != home_code
                or _team(identity["away_code"]) != away_code
                or _name(identity["home_name"]) != _name(ordered["home"]["team"]["name"])
                or _name(identity["away_name"]) != _name(ordered["away"]["team"]["name"])):
            return None
        try:
            original = datetime.strptime(
                f'{identity["date"]} {identity["time"]}', "%b %d, %Y %I:%M%p"
            ).replace(tzinfo=_ET)
        except ValueError:
            return None
        if original.astimezone(UTC) != fixture.kickoff.astimezone(UTC):
            return None
        selected_order = yes[0]["team"]["ordering"]
        return fixture.home_team if selected_order == "home" else fixture.away_team
    return None


def shadow_evidence(
    market: NormalizedMarket, fixture: ShadowFixture, forecast: Forecast,
    *, now: datetime,
) -> Evidence | None:
    """Accept only exact, explicitly scoped, pregame full-game contracts.

    Shape remains provisional until sanitized genuine venue examples are
    checked. Missing/ambiguous metadata produces no evidence, never a guess.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must have an explicit timezone")
    if market.status != "OPEN" or now >= fixture.kickoff:
        return None
    raw = market.original_metadata.get("market")
    if not isinstance(raw, dict):
        return None
    if market.venue not in (Venue.KALSHI, Venue.POLYMARKET):
        return None
    if not market.resolution_rules.strip():
        return None
    # A contradictory contract description cannot be overridden by a label.
    phrases = " ".join(str(v or "").lower() for v in (
        market.title, market.description, market.resolution_rules
    ))
    if any(term in phrases for term in (
        "regulation only", "regulation-time", "three-way", "3-way",
        "first half", "first quarter", "second quarter", "player prop",
    )):
        return None
    selected = _selected_yes(market, raw, fixture)
    if selected is None:
        return None
    if (forecast.model_status != MODEL_STATUS or forecast.home_won is not None
            or forecast.sport != fixture.sport
            or forecast.game_id != fixture.game_id
            or forecast.season != fixture.season
            or forecast.game_date != fixture.kickoff.astimezone(_ET).date()
            or forecast.home_team != fixture.home_team
            or forecast.away_team != fixture.away_team
            or not 0 < forecast.home_win_probability < 1):
        return None
    p_yes = (forecast.home_win_probability if selected == fixture.home_team
             else 1 - forecast.home_win_probability)
    # Deliberately no validation_reference, no CALIBRATED status and no
    # independently corroborating sources. Existing qualify() blocks BUY.
    return Evidence(
        venue=market.venue,
        market_id=market.venue_market_id,
        fair_probability=p_yes,
        source="NBA_NHL_OFFLINE_RESEARCH",
        model_version=_MODEL_VERSION,
        observed_at=now.isoformat(),
        valid_until=min(now + timedelta(minutes=15), fixture.kickoff).isoformat(),
        rules_digest=rules_digest(market),
        review_reference=f"shadow:{fixture.sport}:{fixture.game_id}",
        rationale="Uncalibrated offline Elo probability; not certified or tradeable.",
        validation_reference="",
        validation_status="UNVALIDATED",
        source_independence="RESEARCH_ONLY",
    )


def evaluate_shadow(
    market: NormalizedMarket, fixture: ShadowFixture, forecast: Forecast,
    *, side: Side, now: datetime,
) -> ParallaxPlay | None:
    """Use existing economics/risk qualification, but never emit a BUY."""
    evidence = shadow_evidence(market, fixture, forecast, now=now)
    if evidence is None:
        return None
    play = qualify(market, side, evidence, now=now)
    if play.suggested_action == Action.BUY:
        raise RuntimeError("SHADOW SAFETY STOP: uncertified NBA/NHL BUY")
    return play
