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
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
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
MAX_GAMES = 2  # Strict first-pilot cap: at most four book GETs
MAX_EVENT_PAGES = 3
GUARD_DIR = Path.home() / "parallax-research/nhl"
MIN_SECONDS_BETWEEN_RUNS = 6900  # Two-hour job; blocks reboots and manual replays
MAX_RUNS_PER_UTC_DAY = 12
COOLDOWN_HOURS_ON_DENIAL = 24


class ResearchRunSkipped(RuntimeError):
    """Intentional provider-budget or concurrent-run skip; no API request."""


def _read_guard_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state is not an object")
        return state
    except (OSError, ValueError) as exc:
        raise ResearchRunSkipped("unreadable request-budget state; fail closed") from exc


def _write_guard_state(path: Path, state: dict) -> None:
    """Atomic replacement; never silently reset a previously recorded budget."""
    fd, temp_path = tempfile.mkstemp(prefix=".nhl-budget-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


@contextmanager
def guarded_run(*, now: datetime | None = None):
    """Single-instance, restart-persistent schedule and provider call cap.

    The upper bound assumes --max-games 2 and MAX_EVENT_PAGES=3:
    1 official NHL GET, up to 3 Kalshi event GETs, and 4 book GETs.
    A run counts against the budget BEFORE any network activity starts.
    """
    GUARD_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = GUARD_DIR / ".nhl-shadow.lock"
    state_path = GUARD_DIR / ".nhl-shadow-budget.json"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ResearchRunSkipped("another NHL research scan is running") from exc
        current = (now or datetime.now(UTC)).astimezone(UTC)
        state = _read_guard_state(state_path)
        try:
            cooldown = state.get("cooldown_until_utc")
            if cooldown and current < datetime.fromisoformat(cooldown):
                raise ResearchRunSkipped("provider cooldown active")
            last = state.get("last_start_utc")
            if last and (current - datetime.fromisoformat(last)).total_seconds() < MIN_SECONDS_BETWEEN_RUNS:
                raise ResearchRunSkipped("minimum spacing between research scans")
            count = (
                int(state.get("runs_today", 0))
                if state.get("utc_day") == current.date().isoformat()
                else 0
            )
            if count >= MAX_RUNS_PER_UTC_DAY:
                raise ResearchRunSkipped("daily research scan budget exhausted")
        except (ValueError, TypeError, OverflowError) as exc:
            raise ResearchRunSkipped("invalid request-budget state; fail closed") from exc
        updated = {
            "utc_day": current.date().isoformat(),
            "runs_today": count + 1,
            "last_start_utc": current.isoformat(),
            "cooldown_until_utc": state.get("cooldown_until_utc"),
        }
        _write_guard_state(state_path, updated)
        try:
            yield
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                updated["cooldown_until_utc"] = (
                    datetime.now(UTC) + timedelta(hours=COOLDOWN_HOURS_ON_DENIAL)
                ).isoformat()
                _write_guard_state(state_path, updated)
            raise
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


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



def past_screenings(directory: Path, *, now: datetime) -> dict[str, datetime]:
    """Read archived pregame screening timestamps without extra API requests.

    Archive corruption fails closed rather than accidentally rescreening the
    same two fixtures forever. The newest 1,000 files bound I/O each run;
    records outside seven days do not affect the current weekly slate.
    """
    if not directory.is_dir():
        return {}
    snapshots = sorted(directory.glob("nhl-shadow-*.json"))[-1000:]
    recent: dict[str, datetime] = {}
    cutoff = now - timedelta(days=7)
    for path in snapshots:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or doc.get("mode") != "RESEARCH_ONLY":
                raise ValueError("Invalid research snapshot schema")
            captured = datetime.fromisoformat(doc["captured_at_utc"])
            if captured.utcoffset() is None or captured > now + timedelta(minutes=5):
                raise ValueError("Invalid research snapshot timestamp")
            captured = captured.astimezone(UTC)
            if captured < cutoff:
                continue
            screened = doc.get("screened")
            rejected = doc.get("rejected")
            if not isinstance(screened, list) or not isinstance(rejected, list):
                raise ValueError("Missing screening records")
            for record in screened + rejected:
                ticker = record.get("event_ticker") if isinstance(record, dict) else None
                if not isinstance(ticker, str) or not ticker.startswith(SERIES + "-"):
                    raise ValueError("Invalid screening event ticker")
                old = recent.get(ticker)
                if old is None or captured > old:
                    recent[ticker] = captured
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ResearchRunSkipped(
                f"research snapshot malformed; fail closed: {path.name}"
            ) from exc
    return recent


def rotate_slate(
    matches: list[tuple[datetime, str, ShadowFixture]],
    previous: dict[str, datetime],
    *,
    limit: int,
) -> list[tuple[datetime, str, ShadowFixture]]:
    """Unseen imminent fixtures first; otherwise oldest screening first.

    Eligibility is still established exclusively by independent official
    fixture identity and returned live Kalshi event. This is scheduling only.
    """
    if not 1 <= limit <= MAX_GAMES:
        raise ValueError("Research screen limit out of bounds")
    return sorted(
        matches,
        key=lambda row: (
            row[1] in previous,                    # never screened first
            row[0] if row[1] not in previous else previous[row[1]],
            row[0],                                # deterministic tie breaker
            row[1],
        )
    )[:limit]

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-games", type=int, default=4)
    parser.add_argument("--write-research-snapshot", action="store_true")
    parser.add_argument("--summary-only", action="store_true", help="Avoid unbounded stdout JSON in launchd logs")
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
    previous = past_screenings(RESEARCH_OUTPUT, now=now)
    selected = rotate_slate(matches, previous, limit=args.max_games)
    results, rejected = [], []
    for _, ticker, fixture in selected:
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
        "previously_screened_candidates": sum(ticker in previous for _, ticker, _ in matches),
        "selection_policy": "unseen_earliest_then_oldest_screened",
        "selected_event_tickers": [ticker for _, ticker, _ in selected],
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
    if args.summary_only:
        print(
            f"NHL SHADOW {report['captured_at_utc']}: "
            f"fixtures={len(fixtures)}, matched={len(matches)}, "
            f"screened={len(results)}, rejected={len(rejected)}; "
            "BUY/ORDERS/PUBLISH/ALERTS DISABLED", flush=True
        )
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
        print("NO ORDERS / NO PUBLICATION / NO ALERTS")


if __name__ == "__main__":
    try:
        with guarded_run():
            main()
    except ResearchRunSkipped as exc:
        print(f"NHL SHADOW SKIPPED: {exc}", flush=True)
