"""Freeze November 2 NBA research forecasts and observed PMUS pregame quotes.

Research-only: no model certification, feeds, notifications, or order placement.
One run creates immutable timestamped files and never overwrites older evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from maker_spread_economics.polymarket_us import normalize_book, normalize_market_page
from parallax.fees import attach_fees
from parallax.league_elo_experimental import FinalGame, load_final_games_csv
from parallax.league_market_identity import expected_pmus_nba_slug, selected_yes_team
from parallax.league_model import _logit, _replay, _sigmoid
from parallax.nba_nhl import fetch_nba_fixtures
from parallax.normalization import normalize_pmus
from parallax.economics import pmus_fill_fees

ROOT = Path.home() / "parallax-research/nba"
VALIDATION = ROOT / "validation"
DAY = date(2026, 11, 2)
ET = ZoneInfo("America/New_York")
INTERCEPT = 0.08780963017658344
SLOPE = 0.5181095346239206
HOME_ADVANTAGE = 40.0
K = 30.0


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_once(path: Path, payload: dict) -> None:
    encoded = (json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def saved_forecasts(value: object) -> list[dict]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        matches = [
            rows for rows in value.values()
            if isinstance(rows, list) and rows
            and isinstance(rows[0], dict)
            and {"game_id", "matchup", "kickoff", "home_win_probability"} <= set(rows[0])
        ]
        if len(matches) == 1:
            return matches[0]
    raise ValueError("unrecognized prospective forecast JSON shape")


def main() -> int:
    original_csv = ROOT / "nba-2024-2025-canonical.csv"
    october_json = VALIDATION / "nba-forward-2026-10-22-31-bdl.json"
    original_forecasts = VALIDATION / "nba-prospective-2026-11-01.json"
    now = datetime.now(UTC)
    history = load_final_games_csv(original_csv)
    october = json.loads(october_json.read_text(encoding="utf-8"))
    saved = saved_forecasts(json.loads(original_forecasts.read_text(encoding="utf-8")))
    if len(history) != 2460 or len(october.get("data", [])) != 74:
        raise ValueError("historical source counts changed; refusing to silently freeze")
    if any(g.game_date > date(2026, 4, 12) for g in history):
        raise ValueError("historical source has unreviewed later dates")
    additions = []
    for row in october["data"]:
        d = date.fromisoformat(row["date"][:10])
        h, a = row["home_team"]["abbreviation"], row["visitor_team"]["abbreviation"]
        hs, ass = row["home_team_score"], row["visitor_team_score"]
        if not (
            row.get("status_state") == "final"
            and row.get("postseason") is False
            and row.get("postponed") is False
            and row.get("season") == 2026
            and date(2026, 10, 22) <= d <= date(2026, 10, 31)
            and isinstance(hs, int) and isinstance(ass, int) and hs != ass
        ):
            raise ValueError(f"unverified October final: {row.get('id')}")
        additions.append(FinalGame(
            sport="NBA", game_id=f"bdl:{row['id']}", season=2026,
            game_date=d, home_team=h, away_team=a,
            winner_team=h if hs > ass else a,
        ))
    if len({x.game_id for x in history + additions}) != 2534:
        raise ValueError("duplicate final-game identity")
    _, ratings = _replay(history + additions, HOME_ADVANTAGE, K)
    forecasts = []
    for row in saved:
        kickoff = datetime.fromisoformat(row["kickoff"].replace("Z", "+00:00"))
        if kickoff.tzinfo is None or kickoff.utcoffset() is None:
            raise ValueError("forecast kickoff lacks timezone")
        if kickoff.astimezone(ET).date() != DAY:
            continue
        if now >= kickoff:
            raise ValueError("too late to freeze a prospective November 2 forecast")
        away, home = row["matchup"].split(" @ ")
        h = ratings.get(("NBA", home), 1500.0)
        a = ratings.get(("NBA", away), 1500.0)
        elo = 1 / (1 + 10 ** (-((h + HOME_ADVANTAGE - a) / 400)))
        updated = _sigmoid(INTERCEPT + SLOPE * _logit(elo))
        forecasts.append({
            "game_id": row["game_id"], "matchup": row["matchup"],
            "kickoff": kickoff.astimezone(UTC).isoformat(),
            "home_team": home, "away_team": away,
            "home_probability_original": float(row["home_win_probability"]),
            "home_probability_refreshed": updated,
        })
    if len(forecasts) != 15 or len({x["game_id"] for x in forecasts}) != 15:
        raise ValueError(f"expected exactly 15 unique November 2 forecasts, got {len(forecasts)}")
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    forecast_path = VALIDATION / f"nba-nov2-frozen-forecasts-{stamp}.json"
    report_path = VALIDATION / f"nba-nov2-research-quotes-{stamp}.json"
    freeze = {
        "schema_version": 1, "frozen_at_utc": now.isoformat(), "game_day_et": DAY.isoformat(),
        "model_status": "RESEARCH_NOT_CERTIFIED", "buy_authorized": False,
        "historical_cutoff": "2026-10-31", "data_gaps": [
            "No 2026 playoff results", "No 2026 November 1 results",
            "No offseason ratings regression", "Prospective certification incomplete",
        ],
        "sources": {
            "canonical_csv_sha256": sha256(original_csv),
            "october_finals_sha256": sha256(october_json),
            "original_prospective_sha256": sha256(original_forecasts),
            "canonical_games": len(history), "october_finals": len(additions),
        },
        "parameters_frozen": {
            "home_advantage": HOME_ADVANTAGE, "k": K,
            "platt_intercept": INTERCEPT, "platt_slope": SLOPE,
        },
        "forecasts": sorted(forecasts, key=lambda r: r["kickoff"]),
    }
    write_once(forecast_path, freeze)
    print(f"FROZEN FORECASTS: {len(forecasts)}")
    print(f"FORECAST FILE: {forecast_path}")
    print(f"FORECAST SHA256: {sha256(forecast_path)}", flush=True)

    report = {
        "schema_version": 1, "run_started_at_utc": now.isoformat(),
        "forecast_file": str(forecast_path),
        "forecast_sha256": sha256(forecast_path),
        "research_only": True, "certified": False, "buy_authorized": False,
        "venues": ["POLYMARKET_US"], "fee_model": "Existing PARALLAX taker fee model, 25-share estimate",
        "max_venue_quote_age_seconds": 120,
        "rows": [], "errors": [],
    }
    try:
        fixture_map = {f.game_id: f for f in fetch_nba_fixtures(now=now)}
        with urlopen("https://gateway.polymarket.us/v2/leagues/nba/events", timeout=15) as response:
            data = json.load(response)
        market_map = {
            market["slug"]: market
            for event in data["events"]
            for market in event.get("markets", [])
            if market.get("slug")
        }
        for forecast in freeze["forecasts"]:
            fixture = fixture_map.get(forecast["game_id"])
            if fixture is None or fixture.kickoff.isoformat() != forecast["kickoff"]:
                report["errors"].append({"game_id": forecast["game_id"], "reason": "fixture_missing_or_mismatch"})
                continue
            slug = expected_pmus_nba_slug(fixture)
            raw = market_map.get(slug)
            if raw is None:
                report["errors"].append({"slug": slug, "reason": "venue_market_not_returned"})
                continue
            try:
                row = normalize_market_page({"markets": [raw]})[0]
                initial = normalize_pmus(row, {}, datetime.now(UTC).isoformat())
                yes_team = selected_yes_team(initial, raw, fixture)
                if yes_team is None:
                    raise ValueError("strict_market_identity_rejected")
                with urlopen(f"https://gateway.polymarket.us/v1/markets/{slug}/book", timeout=10) as response:
                    book_payload = json.load(response)
                received = datetime.now(UTC)
                book = normalize_book(book_payload, expected_slug=slug)
                venue_time = book.get("transact_time")
                if not isinstance(venue_time, str):
                    raise ValueError("venue_book_timestamp_missing")
                venue_at = datetime.fromisoformat(venue_time.replace("Z", "+00:00"))
                quote_age = (received - venue_at).total_seconds()
                if venue_at.tzinfo is None or quote_age < -5 or quote_age > 120:
                    raise ValueError("venue_book_timestamp_stale_or_future")
                if received >= fixture.kickoff:
                    raise ValueError("quote_observed_after_tipoff")
                market = attach_fees(normalize_pmus(row, book, received.isoformat()), received)
                fee = market.mechanics
                p_home = forecast["home_probability_refreshed"]
                p_yes = p_home if yes_team == fixture.home_team else 1 - p_home
                sides = []
                for side, probability in (("YES", p_yes), ("NO", 1 - p_yes)):
                    level = book.get(f"{slug}::{side}", {})
                    ask = level.get("best_ask")
                    size = level.get("ask_size_shares")
                    if not isinstance(ask, (int, float)) or not 0 < ask < 1 or not isinstance(size, (int, float)) or size <= 0:
                        sides.append({"side": side, "status": "NO_EXECUTABLE_ASK"})
                        continue
                    verified_fee = fee.fee_status == "VERIFIED_UPPER_BOUND" and fee.fee_rate == 0.0695
                    estimate = (
                        float(pmus_fill_fees([(25, float(ask))], theta=fee.fee_rate)[0]) / 25
                        if verified_fee and size >= 25 else None
                    )
                    net = probability - float(ask) - estimate if estimate is not None else None
                    sides.append({
                        "side": side, "model_probability": probability,
                        "best_ask": ask, "displayed_ask_size": size,
                        "gross_difference": probability - ask,
                        "estimated_fee_per_share_25": estimate, "net_difference_estimate": net,
                        "status": "WATCH_RESEARCH_UNCERTIFIED" if net is not None and net > 0
                                  else ("PASS_RESEARCH" if net is not None else "FEE_OR_DEPTH_UNVERIFIED"),
                        "buy_authorized": False,
                    })
                report["rows"].append({
                    "game_id": forecast["game_id"], "matchup": forecast["matchup"],
                    "kickoff": forecast["kickoff"], "slug": slug,
                    "market_id": str(raw["id"]), "yes_team": yes_team,
                    "venue_book_time": venue_time, "book_received_utc": received.isoformat(),
                    "quote_age_seconds": quote_age, "fee_status": fee.fee_status,
                    "sides": sides,
                })
            except HTTPError as exc:
                report["errors"].append({"slug": slug, "reason": "http_error", "status": exc.code})
            except (URLError, ValueError, TypeError, KeyError, IndexError) as exc:
                report["errors"].append({"slug": slug, "reason": type(exc).__name__, "detail": str(exc)[:120]})
    except (HTTPError, URLError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        report["errors"].append({"reason": "fixture_or_market_discovery_failed",
                                 "error_type": type(exc).__name__})
    report["run_completed_at_utc"] = datetime.now(UTC).isoformat()
    report["books_verified"] = len(report["rows"])
    report["forecast_count"] = len(forecasts)
    report["outcome"] = "PARTIAL" if report["errors"] else "COMPLETE_FOR_RETURNED_MARKETS"
    write_once(report_path, report)
    print(f"QUOTE FILE: {report_path}")
    print(f"QUOTE SHA256: {sha256(report_path)}")
    print(f"BOOKS VERIFIED: {len(report['rows'])}; COVERAGE GAPS: {len(report['errors'])}")
    for row in report["rows"]:
        candidates = [side for side in row["sides"] if side.get("net_difference_estimate") is not None]
        best = max(candidates, key=lambda x: x["net_difference_estimate"]) if candidates else None
        if best:
            print(f"{row['matchup']:12s} {best['side']:3s} net_est={best['net_difference_estimate']:+.1%} RESEARCH ONLY")
    for error in report["errors"]:
        print("GAP:", error.get("slug") or error.get("game_id") or error["reason"], error["reason"])
    print("BUY AUTHORIZATION: NONE | ORDERS: 0 | ALERTS: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
