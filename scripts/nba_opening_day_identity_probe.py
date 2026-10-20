#!/usr/bin/env python3
"""One-shot verified NBA opening-day fixture + contract discovery.

RESEARCH ONLY. Max five source requests: ONE authenticated BALLDONTLIE
date page, TWO Kalshi nested event reads, TWO public Polymarket exact-slug
reads. No retries, order books, orders, polling, alerts or production writes.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from getpass import getpass
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient
from parallax.league_elo_experimental import load_final_games_csv, predict_future
from parallax.league_shadow_evidence import nba_fixture_from_balldontlie

# All identities below are independently backed by inspected public venue
# contract metadata, except BDL game IDs, which this probe must obtain.
GAMES = (
    dict(
        label="PHI@NYK", away="PHI", home="NYK", start="2026-10-20T23:00:00+00:00",
        kalshi_event="KXNBAGAME-26OCT20PHINYK",
        pmus_slug="aec-nba-phi-ny-2026-10-20", pmus_id="1146298",
        pmus_away="PHI", pmus_home="NY",
        venue_away_name="Philadelphia", venue_home_name="New York",
    ),
    dict(
        label="OKC@SAS", away="OKC", home="SAS", start="2026-10-21T01:30:00+00:00",
        kalshi_event="KXNBAGAME-26OCT20OKCSAS",
        pmus_slug="aec-nba-okc-sa-2026-10-20", pmus_id="1146301",
        pmus_away="OKC", pmus_home="SA",
        venue_away_name="Oklahoma City", venue_home_name="San Antonio",
    ),
)


def fetch_json(url: str, *, headers: dict[str, str] | None = None) -> dict:
    # One request only. urllib has no 429-retry loop.
    with urlopen(Request(url, headers=headers or {}), timeout=15) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("Provider response must be an object")
    return payload


def verified_fixtures(data: dict) -> list[dict]:
    rows = data.get("data")
    meta = data.get("meta")
    if (not isinstance(rows, list) or not isinstance(meta, dict)
            or len(rows) > 100 or meta.get("next_cursor") is not None):
        raise ValueError("STOP: incomplete/malformed BDL regular-season date page")
    checked = []
    for cfg in GAMES:
        matches = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("STOP: malformed BDL game")
            home = row.get("home_team") or {}
            away = row.get("visitor_team") or {}
            if (isinstance(home, dict) and isinstance(away, dict)
                    and home.get("abbreviation") == cfg["home"]
                    and away.get("abbreviation") == cfg["away"]):
                matches.append(row)
        if len(matches) != 1:
            raise ValueError(f'STOP: BDL game identity ambiguous/missing for {cfg["label"]}')
        fixture = nba_fixture_from_balldontlie(
            matches[0], regular_season_query_verified=True,
            venue_home_name=cfg["venue_home_name"],
            venue_away_name=cfg["venue_away_name"],
        )
        if (fixture.season != 2026 or fixture.kickoff.isoformat() != cfg["start"]
                or fixture.home_team != cfg["home"]
                or fixture.away_team != cfg["away"]):
            raise ValueError(f'STOP: BDL start/season/teams mismatch {cfg["label"]}')
        checked.append({"config": cfg, "fixture": fixture})
    return checked


def inspect_kalshi(cfg: dict) -> dict:
    url = (
        "https://external-api.kalshi.com/trade-api/v2/events/"
        + cfg["kalshi_event"] + "?with_nested_markets=true"
    )
    data = fetch_json(url)
    event = data.get("event") or {}
    if not isinstance(event, dict) or event.get("event_ticker") != cfg["kalshi_event"]:
        raise ValueError(f'STOP: Kalshi event identity mismatch {cfg["label"]}')
    # Current Kalshi GetEventResponse nests markets INSIDE event.
    markets = event.get("markets")
    if markets is None:
        markets = data.get("markets")
    if not isinstance(markets, list):
        raise ValueError(f'STOP: Kalshi event missing market list {cfg["label"]}')
    selected = []
    for market in markets:
        if not isinstance(market, dict):
            raise ValueError("STOP: malformed Kalshi market")
        if market.get("event_ticker") != cfg["kalshi_event"]:
            raise ValueError("STOP: Kalshi market belongs to another event")
        selected.append({
            "ticker": market.get("ticker"),
            "status": market.get("status"),
            "title": market.get("title"),
            "yes_sub_title": market.get("yes_sub_title"),
            "rules_primary": market.get("rules_primary"),
            "rules_secondary": market.get("rules_secondary"),
        })
    return {"event": cfg["kalshi_event"], "markets": selected}


def inspect_pmus(client: PolymarketUSPublicClient, cfg: dict) -> dict:
    rows = client.markets_page(
        limit=5, offset=0, slugs=[cfg["pmus_slug"]],
        retry_transport_errors=False,
    )
    if len(rows) != 1 or rows[0]["slug"] != cfg["pmus_slug"]:
        raise ValueError(f'STOP: PMUS exact contract missing {cfg["label"]}')
    raw = rows[0]["raw"]
    if (rows[0]["id"] != cfg["pmus_id"] or str(raw.get("id")) != cfg["pmus_id"]
            or raw.get("marketType") != "moneyline"
            or raw.get("status") != "MARKET_STATUS_OPEN"
            or raw.get("gameStartTime") not in (
                cfg["start"].replace("+00:00", "Z"),
                cfg["start"])):
        raise ValueError(f'STOP: PMUS moneyline/time identity mismatch {cfg["label"]}')
    sides = raw.get("marketSides")
    if not isinstance(sides, list) or len(sides) != 2:
        raise ValueError("STOP: PMUS outcome count")
    outcomes = []
    for side in sides:
        team = side.get("team") if isinstance(side, dict) else None
        if not isinstance(team, dict) or type(side.get("long")) is not bool:
            raise ValueError("STOP: PMUS team/YES orientation missing")
        expected = {
            "away": cfg["pmus_away"],
            "home": cfg["pmus_home"],
        }
        ordering = team.get("ordering")
        if (ordering not in expected
                or str(team.get("abbreviation") or "").upper() != expected[ordering]
                or str(team.get("league") or "").lower() != "nba"):
            raise ValueError("STOP: PMUS team ordering/league mismatch")
        outcomes.append({
            "long_yes": side["long"],
            "team_code": team.get("abbreviation"),
            "team_name": team.get("name"),
            "ordering": ordering,
        })
    if len({v["ordering"] for v in outcomes}) != 2 or sum(v["long_yes"] for v in outcomes) != 1:
        raise ValueError("STOP: PMUS YES/NO outcome orientation")
    description = raw.get("description")
    if not isinstance(description, str) or "Overtime is included if played." not in description:
        raise ValueError("STOP: PMUS overtime rules not verified")
    return {
        "id": cfg["pmus_id"],
        "slug": cfg["pmus_slug"],
        "gameStartTime": raw["gameStartTime"],
        "description": description,
        "outcomes": outcomes,
    }


def main() -> None:
    key = getpass("BALLDONTLIE NBA API key (not saved or printed): ").strip()
    if not key:
        raise SystemExit("STOP: NBA source key required; no provider requests")
    params = urlencode({
        "dates[]": "2026-10-20", "seasons[]": "2026",
        "season_type": "regular", "per_page": "100",
    })
    data = fetch_json(
        "https://api.balldontlie.io/v1/games?" + params,
        headers={"Authorization": key},
    )
    checked = verified_fixtures(data)
    games = load_final_games_csv(
        Path.home() / "parallax-research/nba/2025-regular/nba-2025-canonical.csv"
    )
    games = [g for g in games if g.sport == "NBA" and g.season == 2025]
    if len(games) != 1230:
        raise ValueError("STOP: expected verified 2025 NBA history")
    report = {
        "research_only": True,
        "buy_authorized": False,
        "source_calls_budget": {"balldontlie": 1, "kalshi": 2, "polymarket_us": 2},
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "games": [],
    }
    client = PolymarketUSPublicClient(
        minimum_interval_seconds=2, max_requests_per_minute=2,
    )
    try:
        for item in checked:
            cfg, fixture = item["config"], item["fixture"]
            forecast = predict_future(
                games, sport="NBA",
                game_date=fixture.kickoff.astimezone(ZoneInfo("America/New_York")).date(),
                home_team=fixture.home_team, away_team=fixture.away_team,
                game_id=fixture.game_id, season=fixture.season,
            )
            kalshi = inspect_kalshi(cfg)
            pmus = inspect_pmus(client, cfg)
            report["games"].append({
                "matchup": cfg["label"],
                "bdl_game_id": fixture.game_id,
                "tipoff_utc": fixture.kickoff.isoformat(),
                "home_code": fixture.home_team,
                "away_code": fixture.away_team,
                "model_home_win_probability_experimental": round(
                    forecast.home_win_probability, 6
                ),
                "kalshi": kalshi,
                "polymarket_us": pmus,
            })
    finally:
        client.close()
    folder = Path.home() / "parallax-research/nba/forward"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"opening-day-identities-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for game in report["games"]:
        print("\nMATCHUP:", game["matchup"], "BDL:", game["bdl_game_id"])
        print("TIPOFF:", game["tipoff_utc"])
        print("ELO HOME:", game["model_home_win_probability_experimental"])
        print("KALSHI:", json.dumps(game["kalshi"], indent=2))
        print("POLYMARKET:", json.dumps(game["polymarket_us"], indent=2))
    print("\nSAVED:", path)
    print("RESEARCH ONLY / NO BUY / NO QUOTES FETCHED")


if __name__ == "__main__":
    main()
