"""Shared bounded live-acquisition CLI for the NBA and NHL scan lanes."""
from __future__ import annotations

import json
import hashlib
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parallax.league_elo_experimental import FinalGame, load_final_games_csv
from parallax.league_shadow_evidence import ShadowFixture
from parallax.nba_nhl import scan_league, fetch_nba_fixtures, fetch_nhl_fixtures
from parallax.discovery import paginate, paginate_collection
from parallax.sources import KalshiPublicClient
from maker_spread_economics.polymarket_us import PolymarketUSPublicClient
from parallax.pmus_acquisition import PMUSAcquisition
from parallax.alerts import AlertDeliveryStore, AlertDispatcher

KALSHI_EVENT_PAGES = 3
KALSHI_MAX_GAMES = 2
_ET = ZoneInfo("America/New_York")


def _load_existing_history(sport: str, configured_path: Path, *, explicit: bool) -> list[FinalGame]:
    """Prefer configured runtime history, then reuse verified research snapshots."""
    if explicit or configured_path.exists():
        rows = load_final_games_csv(configured_path)
        if not rows:
            raise ValueError(f"{sport} final-results history is empty")
        return rows
    research = Path.home() / "parallax-research"
    if sport == "NBA":
        rows = load_final_games_csv(research / "nba/nba-2024-2025-canonical.csv")
        if not rows:
            raise ValueError("NBA verified final-results history is empty")
        return rows

    root = research / "nhl/historical"
    manifest = json.loads((root / "acquisition-manifest.json").read_text(encoding="utf-8"))
    rows: list[FinalGame] = []
    seen: set[str] = set()
    for season, info in sorted(manifest["seasons"].items()):
        for page in info["pages"]:
            raw = (root / page["filename"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != page["sha256"]:
                raise ValueError("NHL historical snapshot hash mismatch")
            doc = json.loads(raw)
            games = doc.get("data") if isinstance(doc, dict) else None
            if not isinstance(games, list):
                raise ValueError("NHL historical snapshot malformed")
            for game in games:
                home, away = game["home_team"]["tricode"], game["away_team"]["tricode"]
                game_id = f"bdl:{game['id']}"
                if game_id in seen or game["home_score"] == game["away_score"]:
                    raise ValueError("NHL historical snapshot duplicate or tied game")
                seen.add(game_id)
                rows.append(FinalGame(
                    sport="NHL", game_id=game_id, season=int(season),
                    game_date=date.fromisoformat(game["game_date"]),
                    home_team=home, away_team=away,
                    winner_team=home if game["home_score"] > game["away_score"] else away,
                ))
    if len(rows) != 5248 or {game.season for game in rows} != {2022, 2023, 2024, 2025}:
        raise ValueError("NHL verified historical snapshot coverage mismatch")
    return rows


def _live_markets(sport: str, now: datetime, acquisition: PMUSAcquisition, fixtures):
    kalshi = KalshiPublicClient()
    pmus = PolymarketUSPublicClient()
    krows, prows = [], []
    diagnostics = {"KALSHI": {"coverage": {"state": "PARTIAL"}, "discovered": 0},
                   "PMUS": {"coverage": {"state": "PARTIAL"}, "discovered": 0}}
    try:
        upcoming = sorted(
            (fixture for fixture in fixtures if fixture.sport == sport and fixture.kickoff > now),
            key=lambda fixture: fixture.kickoff,
        )
        expected = {}
        for fixture in upcoming:
            date_code = fixture.kickoff.astimezone(_ET).strftime("%y%b%d").upper()
            event_ticker = f"KX{sport}GAME-{date_code}{fixture.away_team}{fixture.home_team}"
            expected[event_ticker] = fixture
        ticker = f"KX{sport}GAME"
        events, ecov = paginate_collection(
            lambda *, limit, cursor: kalshi.events_page(
                series_ticker=ticker, limit=limit, cursor=cursor,
                with_nested_markets=True,
            ),
            key="events", page_size=200, max_pages=KALSHI_EVENT_PAGES,
        )
        matched = sorted(
            (event for event in events if event.get("event_ticker") in expected),
            key=lambda event: expected[event["event_ticker"]].kickoff,
        )[:KALSHI_MAX_GAMES]
        missing_nested = 0
        for event in matched:
            nested = event.get("markets")
            if not isinstance(nested, list) or not nested:
                missing_nested += 1
                continue
            for market in nested:
                if not isinstance(market, dict):
                    continue
                krows.append({
                    **market,
                    "event_ticker": event["event_ticker"],
                    "_discovery_event": event,
                    "_discovery_series": {"ticker": ticker},
                })
        diagnostics["KALSHI"] = {
            "coverage": {
                "state": "BOUNDED" if ecov.state != "PARTIAL" else "PARTIAL",
                "events": ecov.state,
                "fixture_candidates": len(expected),
                "exact_fixture_candidates": len(matched),
                "missing_nested_markets": missing_nested,
                "request_cap": 1 + KALSHI_EVENT_PAGES + 2 * KALSHI_MAX_GAMES,
                "request_count": kalshi.request_count,
            },
            "discovered": len(krows),
        }
        def request_markets():
            rows, _coverage = paginate(
                lambda *, limit, offset: pmus.markets_page(
                    limit=limit, offset=offset, retry_transport_errors=False,
                ),
                page_size=100, max_pages=4, max_rows=400,
            )
            return rows
        all_pmus = acquisition.discover(f"{sport}:bounded-active-universe:v1", request_markets)
        from parallax.discovery import CoverageReport, Coverage
        cov = CoverageReport(Coverage.BOUNDED, 4, len(all_pmus), len(all_pmus), 100, 4,
                             "cached or bounded active-market pagination", False)
        def belongs(row):
            raw = row.get("raw", {})
            text = " ".join(str(v or "") for v in (row.get("question"), row.get("slug"), raw.get("league"), raw.get("sport"), raw.get("marketType"))).casefold()
            return sport.casefold() in text or f"{sport.casefold()}-" in str(row.get("slug", "")).casefold()
        prows = [r for r in all_pmus if belongs(r)]
        diagnostics["PMUS"] = {"coverage": {"state": cov.state, "reason": "bounded active-market pagination; provider has no league filter"}, "discovered": len(prows)}
    except Exception as exc:
        if hasattr(kalshi, "close"): kalshi.close()
        pmus.close()
        diagnostics["KALSHI" if not krows else "PMUS"] = {"coverage": {"state": "PARTIAL", "error_type": type(exc).__name__}, "discovered": 0}
    return krows, prows, diagnostics, kalshi, pmus


def main(sport: str) -> int:
    """Acquire fixtures/markets and scan using local verified final results.

    Fixture JSON may be injected for offline operations; ordinary runs query
    the official league source and existing Kalshi/PMUS clients.
    """
    now = datetime.now(UTC)
    state = Path(os.environ.get("PARALLAX_STATE_DIR", "data/parallax"))
    fixture_path = Path(os.environ.get(f"PARALLAX_{sport}_FIXTURES", state / f"{sport.lower()}_fixtures.json"))
    history_env = os.environ.get(f"PARALLAX_{sport}_HISTORY")
    history_path = Path(history_env) if history_env else state / f"{sport.lower()}_finals.csv"
    try:
        history = _load_existing_history(sport, history_path, explicit=history_env is not None)
        if not os.environ.get(f"PARALLAX_{sport}_FIXTURES"):
            fixtures = fetch_nba_fixtures(now=now) if sport == "NBA" else fetch_nhl_fixtures(now=now)
        else:
            fixtures_raw = json.loads(fixture_path.read_text(encoding="utf-8"))
            fixtures = [ShadowFixture(**{**row, "kickoff": datetime.fromisoformat(row["kickoff"].replace("Z", "+00:00"))}) for row in fixtures_raw]
            if any(row.get("sport") != sport for row in fixtures_raw):
                raise ValueError("fixture cache contains the wrong league")
        acquisition = PMUSAcquisition(sport)
        kalshi_rows, pmus_rows, venue_health, kclient, pclient = _live_markets(sport, now, acquisition, fixtures)
        try:
            dispatcher = AlertDispatcher(AlertDeliveryStore(state / "parallax_alert_delivery.sqlite"))
        except Exception:
            dispatcher = None
        try:
            result = scan_league(sport, fixtures, history, kalshi_rows, pmus_rows,
                kalshi_client=kclient, pmus_client=pclient,
                pmus_acquisition=acquisition, alert_dispatcher=dispatcher, now=now)
        finally:
            if hasattr(kclient, "close"): kclient.close()
            pclient.close()
        scan_venues = result.get("venues", {})
        result["venues"] = venue_health
        for venue in ("KALSHI", "PMUS"):
            result["venues"].setdefault(venue, {}).update({
                "book_failures": scan_venues.get(venue, {}).get("book_failures", 0),
                "book_budget_per_scan": 20,
            })
        if sport == "NHL":
            result["fixture_source"] = "NHL_WEB_SCHEDULE"
        else:
            result["fixture_source"] = "BALLDONTLIE"
        result["scan_status"] = "COMPLETED"
        result["fixture_count"] = len(fixtures)
        result["history_game_count"] = len(history)
        result["history_seasons"] = sorted({game.season for game in history})
        result["history_source"] = (
            "configured_runtime" if history_env is not None or (state / f"{sport.lower()}_finals.csv").exists()
            else "verified_research_snapshot"
        )
        result["matched_event_count"] = venue_health.get("KALSHI", {}).get("coverage", {}).get(
            "exact_fixture_candidates", 0,
        )
    except Exception as exc:
        result = {"read_only": True, "orders": 0, "published": 0, "alerts": 0,
            "sport": sport, "scan_mode": "LIVE_READ_ONLY", "scan_status": "FAILED",
            "failure_type": type(exc).__name__, "refreshed_at": now.isoformat(),
            "fixture_count": 0,
            "market_data_complete": False, "venues": {
            "KALSHI": {"coverage": {"state": "PARTIAL", "error_type": type(exc).__name__}, "discovered": 0},
                "PMUS": {"coverage": {"state": "PARTIAL", "error_type": type(exc).__name__}, "discovered": 0}},
            "summary": {"rows": []}, "rows": []}
        if isinstance(exc, ValueError):
            result["failure_reason"] = str(exc)[:180]
    print(json.dumps(result, default=str, allow_nan=False))
    return 1 if result["scan_status"] == "FAILED" else 0
