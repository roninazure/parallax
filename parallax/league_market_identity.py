"""Fail-closed identity mapping for verified NBA/NHL full-game contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .models import NormalizedMarket, Venue

_ET = ZoneInfo("America/New_York")
_ALLOWED = {"NBA": "KXNBAGAME", "NHL": "KXNHLGAME"}
_KALSHI_RULE = re.compile(
    r"If (?P<winner>.+?) wins the (?P<first>.+?) vs (?P<second>.+?) "
    r"(?P<league>Pro Basketball|NHL) game originally scheduled for "
    r"(?P<date>[A-Z][a-z]{2} [0-9]{1,2}, [0-9]{4}), then the market resolves to Yes\.",
    re.ASCII,
)
_PMUS_RULE = re.compile(
    r"^This market will settle to the winner of the (?P<away_code>[A-Z]{2,4}) "
    r"(?P<away_name>[A-Za-z0-9 ]+?) vs (?P<home_code>[A-Z]{2,4}) "
    r"(?P<home_name>[A-Za-z0-9 ]+?) professional basketball game originally scheduled for "
    r"(?P<date>[A-Z][a-z]{2} [0-9]{1,2}, [0-9]{4}) at "
    r"(?P<time>[0-9]{1,2}:[0-9]{2}[AP]M) ET\. Overtime is included if played\.",
    re.ASCII,
)
_PMUS_CODES = {"GSW": "GS", "NYK": "NY", "SAS": "SA"}


def _name(v: object) -> str:
    return " ".join(v.split()).casefold() if isinstance(v, str) else ""


def _team(v: object) -> str:
    return v.strip().upper() if isinstance(v, str) else ""


def _instant(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v)
        return d.astimezone(UTC) if d.tzinfo and d.utcoffset() is not None else None
    except ValueError:
        return None


def selected_yes_team(
    market: NormalizedMarket, raw: Mapping[str, object], fixture
) -> str | None:
    """Return the exact YES team for an observed and supported contract shape."""
    if market.status != "OPEN" or market.venue not in (Venue.KALSHI, Venue.POLYMARKET):
        return None
    if market.venue == Venue.KALSHI:
        if not fixture.venue_home_name or not fixture.venue_away_name:
            return None
        ticker, event = raw.get("ticker"), raw.get("event_ticker")
        if not isinstance(ticker, str) or not isinstance(event, str):
            return None
        tokens = ticker.split("-")
        if (
            len(tokens) != 3
            or tokens[0] != _ALLOWED[fixture.sport]
            or event != "-".join(tokens[:2])
            or ticker != market.venue_market_id
        ):
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
        names = {
            fixture.home_team: fixture.venue_home_name,
            fixture.away_team: fixture.venue_away_name,
        }
        if selected not in names:
            return None
        selected_name = names[selected]
        if _name(raw.get("title")) != _name(selected_name + " wins") or _name(
            raw.get("yes_sub_title")
        ) != _name(selected_name):
            return None
        primary = raw.get("rules_primary")
        match = _KALSHI_RULE.fullmatch(primary) if isinstance(primary, str) else None
        if not match:
            return None
        rule = match.groupdict()
        league = "Pro Basketball" if fixture.sport == "NBA" else "NHL"
        if (
            rule["league"] != league
            or _name(rule["winner"]) != _name(selected_name)
            or {_name(rule["first"]), _name(rule["second"])}
            != {_name(fixture.venue_home_name), _name(fixture.venue_away_name)}
        ):
            return None
        try:
            original = datetime.strptime(rule["date"], "%b %d, %Y").date()
        except ValueError:
            return None
        return selected if original == local_day else None
    # Only the independently observed PMUS NBA moneyline shape is supported.
    if fixture.sport != "NBA" or raw.get("marketType") != "moneyline":
        return None
    if (
        raw.get("slug") != market.slug
        or str(raw.get("id")) != market.venue_market_id
        or raw.get("status") != "MARKET_STATUS_OPEN"
        or _instant(raw.get("gameStartTime")) != fixture.kickoff.astimezone(UTC)
    ):
        return None
    hc = _PMUS_CODES.get(fixture.home_team, fixture.home_team)
    ac = _PMUS_CODES.get(fixture.away_team, fixture.away_team)
    day = fixture.kickoff.astimezone(_ET).date()
    expected = f"aec-nba-{ac.lower()}-{hc.lower()}-{day.isoformat()}"
    if market.slug != expected:
        return None
    sides = raw.get("marketSides")
    if not isinstance(sides, list) or len(sides) != 2:
        return None
    ordered = {}
    for side in sides:
        if (
            not isinstance(side, dict)
            or type(side.get("long")) is not bool
            or not isinstance(side.get("team"), dict)
        ):
            return None
        team = side["team"]
        order = team.get("ordering")
        if (
            order not in ("home", "away")
            or order in ordered
            or _team(team.get("league")) != "NBA"
        ):
            return None
        code = hc if order == "home" else ac
        if (
            _team(team.get("abbreviation")) != code
            or not _name(team.get("name"))
            or _name(side.get("description")) != _name(team.get("name"))
        ):
            return None
        ordered[order] = side
    if set(ordered) != {"home", "away"}:
        return None
    yes = [s for s in sides if s["long"] is True]
    if len(yes) != 1:
        return None
    for side in sides:
        outcome = "YES" if side["long"] else "NO"
        if _name(market.outcomes.get(outcome)) != _name(side["description"]):
            return None
    rules = raw.get("description")
    match = _PMUS_RULE.match(rules) if isinstance(rules, str) else None
    if not match or rules != market.resolution_rules:
        return None
    identity = match.groupdict()
    if (
        _team(identity["home_code"]) != hc
        or _team(identity["away_code"]) != ac
        or _name(identity["home_name"]) != _name(ordered["home"]["team"]["name"])
        or _name(identity["away_name"]) != _name(ordered["away"]["team"]["name"])
    ):
        return None
    try:
        scheduled = datetime.strptime(
            f"{identity['date']} {identity['time']}", "%b %d, %Y %I:%M%p"
        ).replace(tzinfo=_ET)
    except ValueError:
        return None
    if scheduled.astimezone(UTC) != fixture.kickoff.astimezone(UTC):
        return None
    order = yes[0]["team"]["ordering"]
    return fixture.home_team if order == "home" else fixture.away_team
