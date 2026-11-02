"""Narrow NFL-only The Odds API v4 adapter, isolated from legacy MLB V1.

Only one US-region h2h snapshot is fetched per invocation; no retries.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime
from urllib.parse import urlencode

from .fair_value import BookMoneyline
from .odds_provider import OddsQuota, OddsResponse, SportsEvent, _quote_age_seconds
from .devig import american_to_decimal


NFL_ODDS_ENDPOINT = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/"


def parse_nfl_v4(payload: object, *, now: datetime, headers: object) -> OddsResponse:
    """Parse official v4 schema. Missing quote timestamps fail closed."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone aware")
    if not isinstance(payload, list):
        raise ValueError("expected v4 event array")
    events = []
    for raw in payload:
        if not isinstance(raw, dict) or raw.get("sport_key") != "americanfootball_nfl":
            continue
        home, away = str(raw.get("home_team") or ""), str(raw.get("away_team") or "")
        start = str(raw.get("commence_time") or "")
        if not home or not away or not start:
            continue
        quotes = []
        for book in raw.get("bookmakers") or []:
            if not isinstance(book, dict):
                continue
            name = str(book.get("key") or "").strip()
            if not name:
                continue
            for market in book.get("markets") or []:
                if not isinstance(market, dict) or market.get("key") != "h2h":
                    continue
                timestamp = str(market.get("last_update") or "")
                try:
                    age = _quote_age_seconds(timestamp, now)
                    prices = {item["name"]: item["price"] for item in market.get("outcomes") or []}
                    home_odds = american_to_decimal(float(prices[home]))
                    away_odds = american_to_decimal(float(prices[away]))
                except (KeyError, ValueError, TypeError, OverflowError):
                    continue
                quotes.append(BookMoneyline(name, home_odds, away_odds, age))
        events.append(SportsEvent(str(raw.get("id") or ""), "americanfootball_nfl", "NFL",
                                  home, away, start, tuple(quotes)))

    def header_int(name: str) -> int | None:
        try:
            value = headers.get(name)  # type: ignore[attr-defined]
            return int(value) if value is not None else None
        except (TypeError, ValueError, AttributeError):
            return None

    return OddsResponse(tuple(events), OddsQuota(
        None, header_int("x-requests-used"), header_int("x-requests-remaining"), None))


def fetch_nfl_v4_once(*, api_key: str, now: datetime, timeout: float = 20) -> OddsResponse:
    if not api_key:
        raise ValueError("api_key required")
    url = NFL_ODDS_ENDPOINT + "?" + urlencode({
        "apiKey": api_key, "regions": "us", "markets": "h2h", "oddsFormat": "american",
    })
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "PARALLAX-NFL-research/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError("NFL odds provider returned non-200 status")
        return parse_nfl_v4(json.load(response), now=now, headers=response.headers)
