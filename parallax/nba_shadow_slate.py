"""Pure, research-only NBA slate identity and fair rotation.

No network, credentials, model certification, order access, publisher or alerts.
Candidate event tickers never prove actual contract resolution rules: the
separate league_shadow_evidence adapter must verify every selected market.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Mapping

from .league_shadow_evidence import ShadowFixture, nba_fixture_from_balldontlie

SERIES = "KXNBAGAME"
MAX_GAMES_PER_CYCLE = 2
_MAX_ROWS_PER_PAGE = 100
_MAX_SNAPSHOTS_TO_CHECK = 1000


class ShadowSlateError(ValueError):
    """Incomplete or ambiguous NBA research data; stop without trading."""


def nba_fixtures(
    payload: Mapping[str, object], *, now: datetime, season: int,
    regular_season_query_verified: bool,
) -> dict[str, ShadowFixture]:
    """Index future NBA regular-season BDL fixtures by Kalshi event candidate.

    The query-verified flag may be True only if the caller actually queried
    season_type=regular and checked the source response. BDL team-city names
    are only venue-name candidates: strict venue settlement rules must agree.
    """
    if not regular_season_query_verified or now.tzinfo is None or now.utcoffset() is None:
        raise ShadowSlateError("Verified regular-season query and aware clock required")
    if type(season) is not int or not 2000 <= season <= 2200:
        raise ShadowSlateError("Invalid NBA season")
    if not isinstance(payload, Mapping):
        raise ShadowSlateError("Invalid NBA source response")
    rows, meta = payload.get("data"), payload.get("meta")
    if not isinstance(rows, list) or not isinstance(meta, Mapping) or len(rows) > _MAX_ROWS_PER_PAGE:
        raise ShadowSlateError("Invalid NBA page or excessive fixtures")
    if meta.get("next_cursor") is not None or (
        "next_cursor" not in meta and len(rows) == _MAX_ROWS_PER_PAGE
    ):
        raise ShadowSlateError("NBA fixture pagination incomplete")
    indexed: dict[str, ShadowFixture] = {}
    game_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ShadowSlateError("Malformed NBA fixture row")
        if row.get("season") != season or row.get("postseason") is not False:
            raise ShadowSlateError("NBA season or regular-season fixture mismatch")
        if row.get("postponed") is True or row.get("status_state") != "scheduled":
            continue
        home, away = row.get("home_team"), row.get("visitor_team")
        if not isinstance(home, Mapping) or not isinstance(away, Mapping):
            raise ShadowSlateError("NBA fixture missing independent team records")
        home_city, away_city = home.get("city"), away.get("city")
        if not all(isinstance(x, str) and x.strip() for x in (home_city, away_city)):
            raise ShadowSlateError("NBA fixture missing authoritative city labels")
        try:
            fixture = nba_fixture_from_balldontlie(
                row, venue_home_name=home_city.strip(),
                venue_away_name=away_city.strip(),
                regular_season_query_verified=True,
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise ShadowSlateError("NBA fixture identity failed") from exc
        if fixture.kickoff <= now.astimezone(UTC):
            continue
        date_code = fixture.kickoff.astimezone(ZoneInfo("America/New_York")).strftime("%y%b%d").upper()
        ticker = f"{SERIES}-{date_code}{fixture.away_team}{fixture.home_team}"
        if fixture.game_id in game_ids or ticker in indexed:
            raise ShadowSlateError("Duplicate NBA fixture or venue event candidate")
        game_ids.add(fixture.game_id)
        indexed[ticker] = fixture
    return indexed


def matching_event_candidates(
    fixtures: Mapping[str, ShadowFixture], events: Mapping[str, object],
) -> list[tuple[datetime, str, ShadowFixture]]:
    """Return ticker overlaps only; market/settlement verification still needed."""
    rows = events.get("events") if isinstance(events, Mapping) else None
    if not isinstance(rows, list):
        raise ShadowSlateError("Kalshi event page missing events")
    if events.get("cursor") not in (None, ""):
        raise ShadowSlateError("Kalshi event page incomplete")
    found: dict[str, Mapping] = {}
    for row in rows:
        if not isinstance(row, Mapping) or row.get("series_ticker") != SERIES:
            raise ShadowSlateError("Kalshi series mismatch")
        event = row.get("event_ticker")
        if not isinstance(event, str) or event in found:
            raise ShadowSlateError("Invalid or duplicate Kalshi event")
        found[event] = row
    return sorted(
        ((fixtures[ticker].kickoff, ticker, fixtures[ticker])
         for ticker in fixtures if ticker in found),
        key=lambda row: (row[0], row[1]),
    )


def past_screenings(directory: Path, *, now: datetime) -> dict[str, datetime]:
    """Read recent NBA research receipts, stopping on corrupt archives."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ShadowSlateError("Aware clock required")
    if not directory.exists():
        return {}
    if not directory.is_dir():
        raise ShadowSlateError("NBA archive path not a directory")
    recent: dict[str, datetime] = {}
    cutoff = now.astimezone(UTC) - timedelta(days=7)
    for path in sorted(directory.glob("nba-shadow-*.json"))[-_MAX_SNAPSHOTS_TO_CHECK:]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(document, dict) or document.get("mode") != "RESEARCH_ONLY"
                    or document.get("sport") != "NBA"
                    or document.get("buy_authorized") is not False):
                raise ValueError("Invalid NBA receipt envelope")
            when = datetime.fromisoformat(document["captured_at_utc"].replace("Z", "+00:00"))
            if when.tzinfo is None or when.utcoffset() is None:
                raise ValueError("Missing timestamp timezone")
            when = when.astimezone(UTC)
            if when > now.astimezone(UTC) + timedelta(minutes=5):
                raise ValueError("Future dated archive")
            if not isinstance(document.get("screened"), list) or not isinstance(document.get("rejected"), list):
                raise ValueError("Missing screening records")
            if when < cutoff:
                continue
            for record in document["screened"] + document["rejected"]:
                ticker = record.get("event_ticker") if isinstance(record, dict) else None
                if not isinstance(ticker, str) or not ticker.startswith(f"{SERIES}-"):
                    raise ValueError("Invalid screened event ticker")
                if ticker not in recent or when > recent[ticker]:
                    recent[ticker] = when
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ShadowSlateError(f"NBA receipt is unreadable: {path.name}") from exc
    return recent


def rotate_slate(
    matches: list[tuple[datetime, str, ShadowFixture]],
    previous: Mapping[str, datetime], *, limit: int,
) -> list[tuple[datetime, str, ShadowFixture]]:
    """Screen unseen upcoming games first; then oldest observed candidates."""
    if type(limit) is not int or not 1 <= limit <= MAX_GAMES_PER_CYCLE:
        raise ShadowSlateError("NBA per-cycle game cap exceeded")
    return sorted(
        matches,
        key=lambda row: (
            row[1] in previous,
            row[0] if row[1] not in previous else previous[row[1]],
            row[0], row[1],
        ),
    )[:limit]
