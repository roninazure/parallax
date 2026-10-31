"""Shared bounded live-acquisition CLI for the NBA and NHL scan lanes."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient
from parallax.alerts import AlertDeliveryStore, AlertDispatcher
from parallax.discovery import paginate, paginate_collection
from parallax.league_fixtures import ShadowFixture
from parallax.league_history import load_league_history
from parallax.league_market_identity import expected_pmus_nba_slug, selected_yes_team
from parallax.league_model import load_artifact
from parallax.league_quote_snapshots import LeagueQuoteSnapshotStore
from parallax.nba_nhl import fetch_nba_fixtures, fetch_nhl_fixtures, scan_league
from parallax.normalization import normalize_pmus
from parallax.pmus_acquisition import PMUSAcquisition
from parallax.sources import KalshiPublicClient

KALSHI_EVENT_PAGES = 3
KALSHI_MAX_GAMES = 2
PMUS_NBA_BATCH_SIZE = 12
PMUS_NBA_MAX_FIXTURES = 24
_ET = ZoneInfo("America/New_York")


_load_existing_history = load_league_history


def _discover_nba_pmus(now: datetime, acquisition: PMUSAcquisition, pmus, fixtures):
    """Bounded exact-slug PMUS US lookup; no global Polymarket or book calls.

    SDK MarketsListParams supports slug: list[str]. A missing filtered result
    cannot establish that the exchange has no such market.
    """
    upcoming = sorted(
        (f for f in fixtures if f.sport == "NBA" and f.kickoff > now),
        key=lambda f: (f.kickoff, f.game_id),
    )
    expected = [(expected_pmus_nba_slug(f), f) for f in upcoming]
    diagnostic_rows = [
        {"game_id": f.game_id, "slug": slug, "discovery_state": "NOT_QUERIED_BUDGET",
         "book_state": "NOT_CHECKED"}
        for slug, f in expected
    ]
    coverage = {
        "state": "BOUNDED",
        "method": "PMUS_US_MARKETS_LIST_EXACT_SLUG",
        "reason": "exact-slug filtered US listing; missing results do not prove market absence",
        "fixture_candidates": len(expected),
        "fixture_limit": PMUS_NBA_MAX_FIXTURES,
        "batch_size": PMUS_NBA_BATCH_SIZE,
        "max_provider_requests": 2,
        "queried_fixtures": 0,
        "returned_markets": 0,
        "identity_verified": 0,
    }
    if len({slug for slug, _ in expected}) != len(expected):
        coverage.update(state="PARTIAL", error_type="DuplicateFixtureSlug")
        return [], {"coverage": coverage, "discovered": 0,
                    "fixture_diagnostics": diagnostic_rows}
    selected = expected[:PMUS_NBA_MAX_FIXTURES]
    by_slug = {}
    failed = False
    for start in range(0, len(selected), PMUS_NBA_BATCH_SIZE):
        batch = selected[start:start + PMUS_NBA_BATCH_SIZE]
        slugs = [slug for slug, _ in batch]
        fingerprint = hashlib.sha256("\\n".join(slugs).encode()).hexdigest()
        try:
            rows = acquisition.discover(
                f"NBA:pmus-us-exact-slugs:v1:{fingerprint}",
                lambda slugs=slugs: pmus.markets_page(
                    limit=len(slugs), offset=0, slugs=slugs,
                    retry_transport_errors=False,
                ),
            )
            if (len(rows) > len(slugs)
                    or len({r.get("slug") for r in rows}) != len(rows)
                    or any(r.get("slug") not in slugs for r in rows)):
                raise ValueError("PMUS filtered market response did not match requested slugs")
        except Exception as exc:
            coverage.update(state="PARTIAL", error_type=type(exc).__name__)
            for index in range(start, start + len(batch)):
                diagnostic_rows[index]["discovery_state"] = "DISCOVERY_ERROR"
            failed = True
            break
        coverage["queried_fixtures"] += len(batch)
        coverage["returned_markets"] += len(rows)
        by_slug.update({row["slug"]: row for row in rows})
        for index in range(start, start + len(batch)):
            diagnostic_rows[index]["discovery_state"] = "NOT_RETURNED_UNVERIFIED"
    accepted = []
    for index, (slug, fixture) in enumerate(selected):
        row = by_slug.get(slug)
        if row is None:
            continue
        try:
            market = normalize_pmus(row, {}, now.isoformat())
        except (KeyError, TypeError, ValueError, AttributeError):
            diagnostic_rows[index]["discovery_state"] = "SHAPE_REJECTED"
            continue
        if selected_yes_team(market, market.original_metadata.get("market", {}), fixture) is None:
            diagnostic_rows[index]["discovery_state"] = "IDENTITY_REJECTED"
            continue
        diagnostic_rows[index]["discovery_state"] = "IDENTITY_VERIFIED"
        accepted.append(row)
    if failed or len(selected) < len(expected):
        coverage["state"] = "PARTIAL"
    coverage["identity_verified"] = len(accepted)
    return accepted, {"coverage": coverage, "discovered": len(accepted),
                      "fixture_diagnostics": diagnostic_rows}


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
        if sport == "NBA":
            prows, diagnostics["PMUS"] = _discover_nba_pmus(
                now, acquisition, pmus, fixtures,
            )
        else:
            # NHL has no independently observed PMUS full-game settlement
            # shape. Preserve the existing bounded discovery without promotion.
            def request_markets():
                rows, _coverage = paginate(
                    lambda *, limit, offset: pmus.markets_page(
                        limit=limit, offset=offset, retry_transport_errors=False,
                    ),
                    page_size=100, max_pages=4, max_rows=400,
                )
                return rows
            all_pmus = acquisition.discover(
                f"{sport}:bounded-active-universe:v1", request_markets,
            )
            prows = [
                r for r in all_pmus
                if sport.casefold() in " ".join(str(v or "") for v in (
                    r.get("question"), r.get("slug"), r.get("raw", {}).get("league"),
                    r.get("raw", {}).get("sport"), r.get("raw", {}).get("marketType"),
                )).casefold()
            ]
            diagnostics["PMUS"] = {
                "coverage": {"state": "BOUNDED",
                             "reason": "NHL identity not independently observed; ranked pagination only"},
                "discovered": len(prows),
            }
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
        history = load_league_history(sport, history_path, explicit=history_env is not None)
        artifact_env = os.environ.get(f"PARALLAX_{sport}_MODEL_ARTIFACT")
        artifact_path = Path(artifact_env) if artifact_env else Path(__file__).resolve().parents[1] / "models/parallax" / f"{sport.lower()}_calibrated.json"
        artifact_sha = os.environ.get(f"PARALLAX_{sport}_MODEL_SHA256")
        model_artifact = None
        model_artifact_error = None
        try:
            model_artifact = load_artifact(artifact_path, sport=sport, now=now,
                                           expected_sha256=artifact_sha)
        except FileNotFoundError:
            model_artifact_error = "model artifact missing"
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            model_artifact_error = f"model artifact rejected: {type(exc).__name__}"
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
        quote_store = None
        quote_dir = os.environ.get("PARALLAX_LEAGUE_QUOTE_SNAPSHOT_DIR")
        if quote_dir:
            retention = int(os.environ.get("PARALLAX_LEAGUE_QUOTE_RETENTION_DAYS", "180"))
            quote_store = LeagueQuoteSnapshotStore(quote_dir, retention_days=retention)
        try:
            result = scan_league(sport, fixtures, history, kalshi_rows, pmus_rows,
                kalshi_client=kclient, pmus_client=pclient,
                pmus_acquisition=acquisition, alert_dispatcher=dispatcher,
                quote_snapshot_store=quote_store,
                model_artifact=model_artifact, model_artifact_error=model_artifact_error,
                expected_model_sha256=artifact_sha,
                now=now)
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
        # Fixture-level status is observational. A missing market listing is
        # never upgraded to proof of exchange absence or book availability.
        observed_books = scan_venues.get("PMUS", {}).get("book_states", {})
        for entry in result["venues"].get("PMUS", {}).get("fixture_diagnostics", []):
            if entry["discovery_state"] == "IDENTITY_VERIFIED":
                entry["book_state"] = observed_books.get(entry["slug"], "NOT_CHECKED")
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
            else "research_snapshot"
        )
        result["model_artifact_sha256"] = model_artifact.get("artifact_sha256") if model_artifact else None
        result["matched_event_count"] = venue_health.get("KALSHI", {}).get("coverage", {}).get(
            "exact_fixture_candidates", 0,
        )
        coverage_states = [
            str(venue_health.get(venue, {}).get("coverage", {}).get("state", "PARTIAL"))
            for venue in ("KALSHI", "PMUS")
        ]
        if any(int(venue_health.get(venue, {}).get("book_failures", 0) or 0) > 0
               for venue in ("KALSHI", "PMUS")):
            coverage_states.append("PARTIAL")
        result["source_schedule_state"] = "BOUNDED"
        result["market_coverage_state"] = (
            "COMPLETE" if all(state == "COMPLETE" for state in coverage_states)
            else "BOUNDED" if all(state in {"BOUNDED", "COMPLETE"} for state in coverage_states)
            else "PARTIAL"
        )
        if result.get("model_validation_state") != "CERTIFIED":
            result["opportunity_evaluation_state"] = "MODEL_BLOCKED"
        elif result.get("market_data_complete") is not True or result["market_coverage_state"] != "COMPLETE":
            result["opportunity_evaluation_state"] = "INCOMPLETE_COVERAGE"
        elif any(row.get("publication_eligible") is True for row in result.get("rows", [])):
            result["opportunity_evaluation_state"] = "BUY_CANDIDATE"
        else:
            result["opportunity_evaluation_state"] = "NO_QUALIFYING_BUY"
    except Exception as exc:
        result = {"read_only": True, "orders": 0, "published": 0, "alerts": 0,
            "sport": sport, "scan_mode": "LIVE_READ_ONLY", "scan_status": "FAILED",
            "failure_type": type(exc).__name__, "refreshed_at": now.isoformat(),
            "fixture_count": 0,
            "source_schedule_state": "FAILED",
            "market_coverage_state": "FAILED",
            "opportunity_evaluation_state": "FAILED",
            "market_data_complete": False, "venues": {
            "KALSHI": {"coverage": {"state": "PARTIAL", "error_type": type(exc).__name__}, "discovered": 0},
                "PMUS": {"coverage": {"state": "PARTIAL", "error_type": type(exc).__name__}, "discovered": 0}},
            "summary": {"rows": []}, "rows": []}
        if isinstance(exc, ValueError):
            result["failure_reason"] = str(exc)[:180]
    print(json.dumps(result, default=str, allow_nan=False))
    return 1 if result["scan_status"] == "FAILED" else 0
