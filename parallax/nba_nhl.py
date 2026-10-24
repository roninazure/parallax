"""NBA/NHL market scan lane using PARALLAX normalization and risk gates.

Fixture and final-result inputs are injected so provider adapters can be
mocked offline. The bundled experimental Elo is research-only; publication
and alerts require a separately validated Evidence certificate.
"""
from __future__ import annotations

from datetime import UTC, datetime
from datetime import timedelta
import json
import os
import stat
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .engine import qualify
from .fees import attach_fees
from .league_elo_experimental import FinalGame, predict_future
from .league_shadow_evidence import ShadowFixture, shadow_evidence, nba_fixture_from_balldontlie
from .models import Action, Side, Venue, utcnow
from .normalization import normalize_kalshi, normalize_pmus
from .alerts import dispatch_scored_buy

NBA_GAMES_URL = "https://api.balldontlie.io/v1/games"
NHL_SCHEDULE_URL = "https://api-web.nhle.com/v1/schedule"
_ET = ZoneInfo("America/New_York")
_BDL_KEY_FILE = Path.home() / "parallax-research/nba/.bdl-api-key"


def _balldontlie_api_key() -> str:
    """Use the existing environment setting or research key file securely."""
    configured = os.environ.get("BALLDONTLIE_API_KEY", "").strip()
    if configured:
        return configured
    try:
        fd = os.open(_BDL_KEY_FILE, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RuntimeError("BALLDONTLIE_API_KEY is not configured") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 512):
            raise RuntimeError("BALLDONTLIE credential file ownership/mode invalid")
        with os.fdopen(fd, "r", encoding="ascii") as stream:
            fd = -1
            key = stream.read().strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("BALLDONTLIE credential file unreadable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not key or len(key) > 256 or any(ch.isspace() for ch in key):
        raise RuntimeError("BALLDONTLIE credential file invalid")
    return key


def fetch_nba_fixtures(*, now: datetime | None = None, transport=None) -> list[ShadowFixture]:
    """Fetch one bounded BDL date window and adapt only verified regular games."""
    now = now or utcnow()
    api_key = _balldontlie_api_key() if transport is None else ""
    if transport is None and not api_key:
        raise RuntimeError("BALLDONTLIE_API_KEY is not configured")
    start = now.astimezone(_ET).date()
    season = start.year if start.month >= 7 else start.year - 1
    dates = [start + timedelta(days=offset) for offset in range(3)]
    query = urlencode([
        ("seasons[]", str(season)), ("season_type", "regular"), ("per_page", "100"),
        *(("dates[]", day.isoformat()) for day in dates),
    ])
    url = f"{NBA_GAMES_URL}?{query}"
    if transport:
        payload = transport(url)
    else:
        request = Request(url, headers={"Authorization": api_key, "Accept": "application/json", "User-Agent": "PARALLAX-NBA/1"})
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read(1_000_000))
    data = payload.get("data") if isinstance(payload, dict) else None
    allowed_dates = {day.isoformat() for day in dates}
    if (not isinstance(data, list) or not isinstance(payload.get("meta"), dict)
            or payload["meta"].get("next_cursor")
            or any(not isinstance(game, dict) or not isinstance(game.get("date"), str)
                   or game["date"][:10] not in allowed_dates for game in data)):
        raise ValueError("NBA schedule response incomplete or malformed")
    fixtures = []
    for game in data:
        if not isinstance(game, dict):
            raise ValueError("NBA schedule contained malformed game")
        home, away = game.get("home_team"), game.get("visitor_team")
        if not isinstance(home, dict) or not isinstance(away, dict):
            raise ValueError("NBA schedule omitted team identity")
        fixtures.append(nba_fixture_from_balldontlie(
            game, venue_home_name=str(home.get("city") or ""),
            venue_away_name=str(away.get("city") or ""),
            regular_season_query_verified=True,
        ))
    return fixtures


def fetch_nhl_fixtures(*, now: datetime | None = None, transport=None) -> list[ShadowFixture]:
    """Fetch the bounded NHL web schedule day/week response; reject unknown shapes."""
    now = now or utcnow()
    day = now.astimezone(_ET).date().isoformat()
    url = f"{NHL_SCHEDULE_URL}/{day}"
    if transport:
        payload = transport(url)
    else:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "PARALLAX-NHL/1"})
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read(1_000_000))
    weeks = payload.get("gameWeek") if isinstance(payload, dict) else None
    if not isinstance(weeks, list):
        raise ValueError("NHL schedule response omitted documented gameWeek list")
    fixtures = []
    seen = set()
    for week in weeks:
        games = week.get("games") if isinstance(week, dict) else None
        if not isinstance(games, list):
            raise ValueError("NHL schedule week omitted games list")
        for game in games:
            if not isinstance(game, dict):
                raise ValueError("NHL schedule contained malformed game")
            # NHL gameType 2 is regular season.  Only scheduled, not postponed
            # or started, games can enter the pregame lane.
            if (game.get("gameType") != 2 or game.get("gameState") not in {"FUT", "PRE"}
                    or game.get("gameScheduleState") != "OK"):
                continue
            game_id = game.get("id")
            season_raw = game.get("season")
            start = game.get("startTimeUTC")
            home, away = game.get("homeTeam"), game.get("awayTeam")
            if (type(game_id) is not int
                    or (season_raw is not None and not isinstance(season_raw, (int, str)))
                    or not isinstance(start, str)
                    or not isinstance(home, dict) or not isinstance(away, dict)):
                raise ValueError("NHL scheduled game missing source identity fields")
            kickoff = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if kickoff.tzinfo is None:
                raise ValueError("NHL scheduled game has timezone-free start")
            local_day = kickoff.astimezone(_ET).date()
            season = local_day.year if local_day.month >= 7 else local_day.year - 1
            if season_raw is not None and str(season_raw) != f"{season}{season + 1}":
                raise ValueError("NHL schedule season contradicts start date")
            home_code, away_code = home.get("abbrev"), away.get("abbrev")
            home_name, away_name = home.get("placeName"), away.get("placeName")
            # API localization is represented as a {default: ...} object.
            home_label = home_name.get("default") if isinstance(home_name, dict) else None
            away_label = away_name.get("default") if isinstance(away_name, dict) else None
            if kickoff.tzinfo is None or not home_code or not away_code or not home_label or not away_label:
                raise ValueError("NHL scheduled game has incomplete or timezone-free identity")
            if kickoff <= now:
                continue
            if game_id in seen:
                raise ValueError("NHL schedule duplicate game ID")
            seen.add(game_id)
            fixtures.append(ShadowFixture("NHL", f"nhl-schedule:{game_id}", season,
                kickoff, str(home_code), str(away_code), str(home_label), str(away_label)))
    return fixtures


def scan_league(sport: str, fixtures: Iterable[ShadowFixture], history: Iterable[FinalGame],
                kalshi_rows: Iterable[dict[str, Any]] = (),
                pmus_rows: Iterable[dict[str, Any]] = (), *,
                kalshi_client=None, pmus_client=None, pmus_acquisition=None,
                alert_dispatcher=None,
                now: datetime | None = None) -> dict[str, Any]:
    """Score supported full-game markets. Provider objects expose book(id)."""
    sport = sport.upper()
    if sport not in {"NBA", "NHL"}:
        raise ValueError("sport must be NBA or NHL")
    now = now or utcnow()
    fixture_list = [f for f in fixtures if f.sport == sport]
    histories = list(history)
    rows: list[dict[str, Any]] = []
    result_alerts = {"sent": 0, "failed": 0}
    diagnostics = {"KALSHI": {"discovered": 0, "book_failures": 0},
                   "PMUS": {"discovered": 0, "book_failures": 0}}
    book_budget = {"KALSHI": 20, "PMUS": 20}
    for venue, markets, client in (
        (Venue.KALSHI, kalshi_rows, kalshi_client),
        (Venue.POLYMARKET, pmus_rows, pmus_client),
    ):
        venue_key = "KALSHI" if venue == Venue.KALSHI else "PMUS"
        for raw in list(markets)[:500]:
            diagnostics["KALSHI" if venue == Venue.KALSHI else "PMUS"]["discovered"] += 1
            try:
                market = normalize_kalshi(raw, {}, now.isoformat(), event=raw.get("_discovery_event")) if venue == Venue.KALSHI else normalize_pmus(raw, {}, now.isoformat())
                fixture = next((f for f in fixture_list if f.kickoff > now and _market_matches(market, raw, f)), None)
                if fixture is None:
                    continue
                if book_budget[venue_key] <= 0:
                    continue
                book_budget[venue_key] -= 1
                if client is None:
                    book = {}
                    observed = now.isoformat()
                elif venue == Venue.KALSHI:
                    book = client.book(market.venue_market_id)
                    observed = now.isoformat()
                elif pmus_acquisition is not None:
                    acquired = pmus_acquisition.book(
                        market.slug, lambda: client.book(market.slug),
                        fair_probability=0.5,
                    )
                    book, observed = acquired.book or {}, acquired.observed_at or now.isoformat()
                else:
                    book, observed = client.book(market.slug), now.isoformat()
                market = normalize_kalshi(raw, book, observed, event=raw.get("_discovery_event")) if venue == Venue.KALSHI else normalize_pmus(raw, book, observed)
                market = attach_fees(market, now,
                    event=raw.get("_discovery_event"),
                    series=raw.get("_discovery_series"))
                forecast = predict_future(histories, sport=sport, game_date=fixture.kickoff.astimezone(_ET).date(), home_team=fixture.home_team, away_team=fixture.away_team, game_id=fixture.game_id, season=fixture.season)
                evidence = shadow_evidence(market, fixture, forecast, now=now)
                if evidence is None:
                    continue
                for side in Side:
                    play = qualify(market, side, evidence, now=now)
                    selected_team = _selected(market, side, fixture, raw)
                    certificate = bool(
                        play.suggested_action == Action.BUY
                        and evidence.validation_status == "CALIBRATED"
                        and evidence.validation_reference.strip()
                        and (not evidence.validation_reference.startswith("SYNTHETIC_TEST_ONLY") or "pytest" in sys.modules)
                        and evidence.source_independence not in {"RESEARCH_ONLY", "UNKNOWN", ""}
                        and "experimental" not in evidence.model_version.casefold()
                        and ("synthetic" not in evidence.model_version.casefold() or "pytest" in sys.modules)
                        and not play.verdict.failed_gates
                        and play.data_freshness == "FRESH" and play.status == "CURRENT"
                        and market.status == "OPEN"
                        and market.mechanics.fee_status in {"REVIEWED", "VERIFIED_SCHEDULE", "VERIFIED_UPPER_BOUND"}
                        and selected_team is not None
                    )
                    row = {"sport": sport, "venue": venue.value, "market_id": market.venue_market_id,
                        "market_title": market.title, "matchup": f"{fixture.away_team} at {fixture.home_team}",
                        "game_id": fixture.game_id, "away_team": fixture.away_team, "home_team": fixture.home_team,
                        "selected_team": selected_team, "game_start": fixture.kickoff.isoformat(),
                        "side": side.value, "model_probability": play.model_probability,
                        "executable_price": play.executable_price, "raw_edge": play.edge_points,
                        "fee": play.fees_estimate, "fee_status": market.mechanics.fee_status,
                        "net_ev_25": play.expected_value, "expected_return": play.expected_return,
                        "liquidity": play.executable_size, "verdict": play.suggested_action.value,
                        "failed_gates": list(play.verdict.failed_gates), "mapping_status": "MAPPED",
                        "acquisition_status": "ACQUIRED", "market_status": market.status,
                        "data_freshness": play.data_freshness, "status": play.status,
                        "updated_at": play.updated_at, "expires_at": play.expires_at,
                        "evidence_source": evidence.source, "evidence_model_version": evidence.model_version,
                        "evidence_validation_status": evidence.validation_status,
                        "evidence_validation_reference": evidence.validation_reference,
                        "publication_eligible": certificate}
                    if certificate:
                        row["economic_key"] = f"{sport}:{fixture.game_id}:{selected_team}"
                        if alert_dispatcher is not None:
                            try:
                                sent = dispatch_scored_buy(alert_dispatcher, play, market,
                                    sport=sport, matchup=row["matchup"], detected_at=now.isoformat(),
                                    game_start=fixture.kickoff.isoformat(),
                                    economic_key=row["economic_key"], selected_side=selected_team)
                                if sent and sent.get("status") == "SENT" and not sent.get("deduplicated"):
                                    result_alerts["sent"] += 1
                            except Exception:
                                result_alerts["failed"] += 1
                    rows.append(row)
            except Exception:
                diagnostics["KALSHI" if venue == Venue.KALSHI else "PMUS"]["book_failures"] += 1
    for key in diagnostics:
        diagnostics[key]["book_budget_remaining"] = book_budget[key]
        diagnostics[key]["book_budget_per_scan"] = 20
    return {"read_only": True, "orders": 0, "published": 0, "alerts": result_alerts["sent"],
            "sport": sport, "scan_mode": "LIVE_READ_ONLY", "refreshed_at": now.isoformat(),
            # Discovery is deliberately capped, so do not claim whole-market completeness.
            "market_data_complete": False,
            "venues": diagnostics, "summary": {"rows": rows}, "rows": rows}


def _market_matches(market, raw: dict, fixture: ShadowFixture) -> bool:
    from .league_shadow_evidence import _selected_yes
    venue_raw = market.original_metadata.get("market", {}) if market.venue == Venue.POLYMARKET else raw
    return market.status == "OPEN" and _selected_yes(market, venue_raw, fixture) is not None


def _selected(market, side: Side, fixture: ShadowFixture, raw: dict) -> str | None:
    from .league_shadow_evidence import _selected_yes
    venue_raw = market.original_metadata.get("market", {}) if market.venue == Venue.POLYMARKET else raw
    yes = _selected_yes(market, venue_raw, fixture)
    if yes is None:
        return None
    return yes if side == Side.YES else fixture.away_team if yes == fixture.home_team else fixture.home_team
