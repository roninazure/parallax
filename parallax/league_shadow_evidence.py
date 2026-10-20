"""Isolated NBA/NHL -> existing PARALLAX Evidence/qualify seam.

RESEARCH / SHADOW ONLY. No provider calls, certification, scheduler, public
feed, alerts, or order operations. Unverified venue shapes fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .engine import qualify
from .league_elo_experimental import Forecast, MODEL_STATUS
from .models import Action, Evidence, NormalizedMarket, ParallaxPlay, Side, Venue
from .normalization import rules_digest

_ET = ZoneInfo("America/New_York")
_MODEL_VERSION = "nba-nhl-elo-experimental-v0"
_ALLOWED = {"NBA": "KXNBAGAME-", "NHL": "KXNHLGAME-"}


@dataclass(frozen=True)
class ShadowFixture:
    sport: str
    game_id: str
    season: int
    kickoff: datetime  # Authoritative, timezone-aware actual tipoff / puck drop
    home_team: str      # Canonical abbreviation, not display name
    away_team: str

    def __post_init__(self) -> None:
        if (self.sport not in _ALLOWED or not self.game_id
                or not self.home_team or not self.away_team
                or self.home_team == self.away_team
                or self.kickoff.tzinfo is None
                or self.kickoff.utcoffset() is None):
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


def _selected_yes(market: NormalizedMarket, raw: dict, fixture: ShadowFixture) -> str | None:
    participants = {fixture.home_team, fixture.away_team}
    if market.venue == Venue.KALSHI:
        ticker = raw.get("ticker")
        if (not isinstance(ticker, str) or ticker != market.venue_market_id
                or not ticker.upper().startswith(_ALLOWED[fixture.sport])):
            return None
        # No title substring guessing. The final ticker token must be a team.
        selected = _team(ticker.rsplit("-", 1)[-1])
        return selected if selected in participants else None
    if market.venue == Venue.POLYMARKET:
        sides = raw.get("marketSides")
        if not isinstance(sides, list) or len(sides) != 2:
            return None
        yes = [s for s in sides if isinstance(s, dict) and s.get("long") is True]
        if len(yes) != 1 or not isinstance(yes[0].get("team"), dict):
            return None
        selected = _team(yes[0]["team"].get("abbreviation"))
        return selected if selected in participants else None
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
    if (_team(raw.get("league")) != fixture.sport
            or str(raw.get("marketType") or "").strip().lower()
            not in {"moneyline", "full_game_winner"}
            or _team(raw.get("homeTeam")) != fixture.home_team
            or _team(raw.get("awayTeam")) != fixture.away_team
            or _instant(raw.get("gameStartTime")) != fixture.kickoff.astimezone(UTC)):
        return None
    # Without explicit NHL overtime/shootout settlement semantics, STOP.
    expected_scope = "full_game_including_ot_so" if fixture.sport == "NHL" else "full_game"
    if str(raw.get("settlement_scope") or "").strip().lower() != expected_scope:
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
