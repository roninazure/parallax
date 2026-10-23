#!/usr/bin/env python3
"""Bounded, research-only NHL slate screening. No production side effects.

One NHL schedule GET; up to three paginated Kalshi event GETs; at most
2 * --max-games Kalshi order-book GETs. No retries, credentials, orders,
notifications, publisher calls, scheduler changes, or BUY authorization.

An event ticker is only a candidate. Evidence requires verified, exact
Kalshi settlement rules and independent NHL fixture/place-name identity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from parallax.fees import attach_fees
from parallax.league_elo_experimental import FinalGame, predict_future
from parallax.league_shadow_evidence import ShadowFixture, evaluate_shadow
from parallax.models import Side
from parallax.normalization import normalize_kalshi

ET = ZoneInfo("America/New_York")
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
NHL = "https://api-web.nhle.com/v1"
SERIES = "KXNHLGAME"
HISTORY = Path.home() / "parallax-research/nhl/historical"
RESEARCH_OUTPUT = Path.home() / "parallax-research/nhl/forward"
MAX_GAMES = 8
MAX_EVENT_PAGES = 3

# Verified against the 2026-10-23 MTL@CHI Kalshi event and the official NHL
# schedule. Do not generalize accent removal to every team/contract identity.
_VERIFIED_KALSHI_PLACE_ALIASES = {
    ("MTL", "Montréal"): "Montreal",
}


def kalshi_place_name(code: str, official_place: str) -> str:
    """Resolve only explicitly observed NHL -> Kalshi place-name differences."""
    return _VERIFIED_KALSHI_PLACE_ALIASES.get((code, official_place), official_place)


def fetch_json(url: str) -> dict:
    """One public request. No retries or credentials."""
    req = urllib.request.Request(
        url, headers={
            "Accept": "application/json",
            "User-Agent": "PARALLAX-Research/0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        result = json.load(r)
    if not isinstance(result, dict):
        raise ValueError("Unexpected provider payload")
    return result


def source_name(team: dict) -> str:
    place = team.get("placeName")
    name = place.get("default") if isinstance(place, dict) else ""
    if not isinstance(name, str):
        return ""
    name = " ".join(name.split())
    return name if re.fullmatch(r"[A-Za-zÀ-ÿ .'-]{2,48}", name) else ""


def nhl_fixtures(schedule: dict, now: datetime) -> dict[str, ShadowFixture]:
    """Create exact ET ticker candidates only from future NHL regular games."""
    days = schedule.get("gameWeek")
    if not isinstance(days, list):
        raise ValueError("Official NHL schedule has no gameWeek")
    fixtures: dict[str, ShadowFixture] = {}
    game_ids: set[str] = set()
    for day in days:
        for game in day.get("games", []):
            if game.get("gameType") != 2 or game.get("gameState") not in ("FUT", "PRE"):
                continue
            raw_id = game.get("id")
            if not isinstance(raw_id, int) or raw_id <= 0:
                raise ValueError("Missing authoritative NHL game ID")
            start = datetime.fromisoformat(
                game["startTimeUTC"].replace("Z", "+00:00")
            )
            if start.utcoffset() is None:
                raise ValueError("NHL start is not timezone-aware")
            if start <= now:
                continue
            away = game["awayTeam"]
            home = game["homeTeam"]
            a, h = away["abbrev"], home["abbrev"]
            if not all(
                isinstance(code, str) and re.fullmatch(r"[A-Z]{3}", code)
                for code in (a, h)
            ):
                raise ValueError("Invalid official NHL team code")
            an = kalshi_place_name(a, source_name(away))
            hn = kalshi_place_name(h, source_name(home))
            if not an or not hn or an.casefold() == hn.casefold():
                raise ValueError("NHL official venue place name missing or ambiguous")
            local_day = start.astimezone(ET).date()
            season = local_day.year if local_day.month >= 7 else local_day.year - 1
            season_raw = game.get("season")
            if season_raw is not None and str(season_raw) != f"{season}{season + 1}":
                raise ValueError("NHL fixture season contradicts start date")
            ticker = f"{SERIES}-{local_day:%y%b%d}{a}{h}".upper()
            game_id = f"nhl:{raw_id}"
            if ticker in fixtures or game_id in game_ids:
                raise ValueError("Duplicate NHL game/market candidate")
            game_ids.add(game_id)
            fixtures[ticker] = ShadowFixture(
                sport="NHL", game_id=game_id, season=season,
                kickoff=start.astimezone(UTC),
                home_team=h, away_team=a,
                venue_home_name=hn, venue_away_name=an,
            )
    return fixtures


def kalshi_events() -> tuple[dict[str, dict], int]:
    events: dict[str, dict] = {}
    cursor = ""
    for page in range(1, MAX_EVENT_PAGES + 1):
        params = {
            "series_ticker": SERIES, "status": "open",
            "limit": 200, "with_nested_markets": "true",
        }
        if cursor:
            params["cursor"] = cursor
        response = fetch_json(
            KALSHI + "/events?" + urllib.parse.urlencode(params)
        )
        rows = response.get("events")
        if not isinstance(rows, list):
            raise ValueError("Kalshi event listing is malformed")
        for row in rows:
            if not isinstance(row, dict) or row.get("series_ticker") != SERIES:
                raise ValueError("Kalshi event series mismatch")
            ticker = row.get("event_ticker")
            if not isinstance(ticker, str) or ticker in events:
                raise ValueError("Duplicate or missing Kalshi event ticker")
            events[ticker] = row
        cursor = response.get("cursor") or ""
        if not cursor:
            return events, page
    raise ValueError("STOP: Kalshi event pagination incomplete")


def load_history(directory: Path) -> list[FinalGame]:
    manifest = json.loads((directory / "acquisition-manifest.json").read_text())
    rows: list[FinalGame] = []
    seen: set[str] = set()
    for season, info in sorted(manifest["seasons"].items()):
        for page in info["pages"]:
            raw = (directory / page["filename"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != page["sha256"]:
                raise ValueError("STOP: historical snapshot hash mismatch")
            for game in json.loads(raw)["data"]:
                h = game["home_team"]["tricode"]
                a = game["away_team"]["tricode"]
                game_id = f"bdl:{game['id']}"
                if game_id in seen:
                    raise ValueError("STOP: duplicate historical game")
                seen.add(game_id)
                rows.append(FinalGame(
                    sport="NHL", game_id=game_id, season=int(season),
                    game_date=date.fromisoformat(game["game_date"]),
                    home_team=h, away_team=a,
                    winner_team=h if game["home_score"] > game["away_score"] else a,
                ))
    if len(rows) != 5248 or {g.season for g in rows} != {2022, 2023, 2024, 2025}:
        raise ValueError("STOP: expected four verified NHL seasons / 5248 finals")
    return rows


def evaluate_fixture(fixture: ShadowFixture, event: dict, history: list[FinalGame]) -> dict:
    expected = {
        f"{event['event_ticker']}-{fixture.home_team}",
        f"{event['event_ticker']}-{fixture.away_team}",
    }
    markets = event.get("markets")
    if (
        event.get("series_ticker") != SERIES
        or not isinstance(markets, list)
        or len(markets) != 2
        or {m.get("ticker") for m in markets if isinstance(m, dict)} != expected
    ):
        raise ValueError("Market pair missing, ambiguous or wrong team")

    prediction = predict_future(
        history, sport="NHL",
        game_date=fixture.kickoff.astimezone(ET).date(),
        home_team=fixture.home_team, away_team=fixture.away_team,
        game_id=fixture.game_id, season=fixture.season,
    )
    positions: list[dict] = []
    for raw in markets:
        ticker = raw["ticker"]
        if raw.get("event_ticker") != event["event_ticker"]:
            raise ValueError("Market event identity mismatch")
        book = fetch_json(
            KALSHI + "/markets/" + urllib.parse.quote(ticker, safe="")
            + "/orderbook?depth=5"
        )
        now = datetime.now(UTC)
        if now >= fixture.kickoff:
            raise ValueError("Pregame deadline passed")
        market = normalize_kalshi(raw, book, now.isoformat(), event=event)
        market = attach_fees(market, now)
        for side in (Side.YES, Side.NO):
            play = evaluate_shadow(market, fixture, prediction, side=side, now=now)
            if play is None:
                raise ValueError("Market/rules/fixture verification rejected")
            if play.suggested_action.value == "BUY":
                raise RuntimeError("SHADOW SAFETY STOP: unexpected BUY")
            ask = market.yes_ask if side == Side.YES else market.no_ask
            depth = market.executable_depth.get(side, ())
            size = sum(q for price, q in depth if ask is not None and price == ask)
            positions.append({
                "ticker": ticker,
                "side": side.value,
                "outcome": fixture.home_team if (
                    (ticker.endswith(f"-{fixture.home_team}")) == (side == Side.YES)
                ) else fixture.away_team,
                "action": play.suggested_action.value,
                "ask": ask,
                "available_at_ask": size,
                "gross_edge_pp": play.edge_points,
                "failed_gates": list(play.verdict.failed_gates),
                "fee_status": market.mechanics.fee_status,
            })
    return {
        "game_id": fixture.game_id,
        "matchup": f"{fixture.away_team}@{fixture.home_team}",
        "start_utc": fixture.kickoff.isoformat(),
        "event_ticker": event["event_ticker"],
        "raw_elo_home_probability": round(prediction.home_win_probability, 6),
        "positions": positions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-games", type=int, default=4)
    parser.add_argument("--write-research-snapshot", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_games <= MAX_GAMES:
        parser.error(f"--max-games must be between 1 and {MAX_GAMES}")
    now = datetime.now(UTC)
    schedule = fetch_json(f"{NHL}/schedule/{now.astimezone(ET).date()}")
    fixtures = nhl_fixtures(schedule, now)
    events, pages = kalshi_events()
    matches = sorted(
        ((f.kickoff, ticker, f) for ticker, f in fixtures.items() if ticker in events),
        key=lambda row: row[:2],
    )
    # Keep every unlisted fixture visible in the report; don't invent matches.
    unmatched = sorted(set(fixtures) - set(events))
    history = load_history(HISTORY)
    results, rejected = [], []
    for _, ticker, fixture in matches[:args.max_games]:
        try:
            results.append(evaluate_fixture(fixture, events[ticker], history))
        except (ValueError, KeyError, TypeError) as exc:
            rejected.append({
                "event_ticker": ticker,
                "reason": type(exc).__name__ + ": " + str(exc)[:140],
            })
    report = {
        "mode": "RESEARCH_ONLY",
        "buy_authorized": False,
        "publish_authorized": False,
        "orders_authorized": False,
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "history": "verified 2022-2025 NHL only; current-season results absent",
        "model": "EXPERIMENTAL_NOT_CERTIFIED / raw Elo (calibration not integrated)",
        "fees": "UNVERIFIED_UNTIL_POLICY_REVIEW",
        "fixture_count": len(fixtures),
        "kalshi_event_count": len(events),
        "kalshi_pages": pages,
        "matched_candidates": len(matches),
        "unmatched_tickers": unmatched,
        "screened_count": len(results) + len(rejected),
        "screen_limit": args.max_games,
        "screened": results,
        "rejected": rejected,
        "request_budget_max": 1 + MAX_EVENT_PAGES + 2 * args.max_games,
    }
    if args.write_research_snapshot:
        RESEARCH_OUTPUT.mkdir(parents=True, exist_ok=True)
        filename = "nhl-shadow-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json"
        out = RESEARCH_OUTPUT / filename
        with out.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
        print("RESEARCH SNAPSHOT:", out)
    print(json.dumps(report, indent=2, sort_keys=True))
    print("NO ORDERS / NO PUBLICATION / NO ALERTS")


if __name__ == "__main__":
    main()
