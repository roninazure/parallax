#!/usr/bin/env python3
"""Bounded, unattended-capable NBA Kalshi shadow research. NOT a trading service.

At most one BALLDONTLIE regular-season date-range GET, three paginated Kalshi
events GETs, and four Kalshi books GETs per run. No retries, customer
publication, orders, alerts or automatic installation. Two games maximum.
An uncalibrated Elo forecast is NEVER a certified net trading edge.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from parallax.fees import attach_fees
from parallax.league_elo_experimental import MODEL_STATUS, load_final_games_csv, predict_future
from parallax.league_shadow_evidence import ShadowFixture, evaluate_shadow
from parallax.models import Side
from parallax.nba_shadow_slate import (
    MAX_GAMES_PER_CYCLE, SERIES, nba_fixtures, matching_event_candidates,
    past_screenings, rotate_slate,
)
from parallax.normalization import normalize_kalshi

ET = ZoneInfo("America/New_York")
BDL_URL = "https://api.balldontlie.io/v1/games"
KALSHI_URL = "https://external-api.kalshi.com/trade-api/v2"
ROOT = Path.home() / "parallax-research/nba"
HISTORY_CSV = ROOT / "nba-2024-2025-canonical.csv"
RESEARCH_OUTPUT = ROOT / "forward"
KEY_FILE = ROOT / ".bdl-api-key"
BUDGET_FILE = ROOT / ".nba-shadow-budget.json"
LOCK_FILE = ROOT / ".nba-shadow.lock"
MAX_EVENT_PAGES = 3
MAX_RUNS_PER_UTC_DAY = 12
MIN_RUN_SPACING_SECONDS = 6900
DENIAL_COOLDOWN_HOURS = 24
MAX_TOTAL_GETS = 1 + MAX_EVENT_PAGES + 2 * MAX_GAMES_PER_CYCLE


class ScanSkipped(RuntimeError):
    """Intentionally fail closed before requesting provider data."""


def read_provider_key(path: Path = KEY_FILE) -> str:
    """Local user-owned regular file, mode 0600, no symlinks, no key in logs."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ScanSkipped("NBA provider credential file unavailable") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 512):
            raise ScanSkipped("NBA provider credential file ownership/mode invalid")
        with os.fdopen(fd, "r", encoding="ascii") as stream:
            fd = -1
            value = stream.read().strip()
    except (OSError, UnicodeError) as exc:
        raise ScanSkipped("NBA provider credential unreadable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not value or len(value) > 256 or any(ch.isspace() for ch in value):
        raise ScanSkipped("NBA provider credential invalid")
    return value


def _read_budget() -> dict:
    if not BUDGET_FILE.exists():
        return {}
    try:
        data = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Budget is not an object")
        return data
    except (OSError, ValueError) as exc:
        raise ScanSkipped("NBA request budget unreadable; fail closed") from exc


def _save_budget(data: dict) -> None:
    fd, path = tempfile.mkstemp(prefix=".nba-budget-", dir=ROOT)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, BUDGET_FILE)
    finally:
        if os.path.exists(path):
            os.unlink(path)


@contextmanager
def guarded_run(*, now: datetime | None = None):
    """Exclusive lock and persistent pre-debit for MAX_TOTAL_GETS per run."""
    ROOT.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ScanSkipped("Another NBA shadow scan is running") from exc
        try:
            moment = (now or datetime.now(UTC)).astimezone(UTC)
            old = _read_budget()
            try:
                cooldown = old.get("cooldown_until_utc")
                if cooldown and moment < datetime.fromisoformat(cooldown):
                    raise ScanSkipped("NBA provider-denial cooldown active")
                previous = old.get("last_start_utc")
                if previous and (moment - datetime.fromisoformat(previous)).total_seconds() < MIN_RUN_SPACING_SECONDS:
                    raise ScanSkipped("NBA minimum scan spacing active")
                count = int(old.get("runs_today", 0)) if old.get("utc_day") == moment.date().isoformat() else 0
                if count < 0 or count >= MAX_RUNS_PER_UTC_DAY:
                    raise ScanSkipped("NBA daily request budget exhausted")
            except (ValueError, TypeError, OverflowError) as exc:
                raise ScanSkipped("NBA provider budget invalid") from exc
            _save_budget({
                "utc_day": moment.date().isoformat(),
                "runs_today": count + 1,
                "last_start_utc": moment.isoformat(),
                "max_gets_per_run": MAX_TOTAL_GETS,
                "cooldown_until_utc": cooldown,
            })
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def provider_denied(*, now: datetime | None = None) -> None:
    """Called while guarded lock is held; persist cooldown without retry."""
    state = _read_budget()
    until = (now or datetime.now(UTC)).astimezone(UTC) + timedelta(hours=DENIAL_COOLDOWN_HOURS)
    state["cooldown_until_utc"] = until.isoformat()
    _save_budget(state)


def fetch_json(url: str, *, key: str | None = None) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "PARALLAX-NBA-Shadow/0.1"}
    if key is not None:
        if not url.startswith("https://api.balldontlie.io/v1/games?"):
            raise ValueError("NBA key can only be sent to approved games endpoint")
        headers["Authorization"] = key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 429):
            provider_denied()
        raise
    if not isinstance(payload, dict):
        raise ValueError("Unexpected provider JSON")
    return payload


def bdl_fixtures(*, key: str, now: datetime) -> tuple[dict[str, ShadowFixture], int]:
    """One verified three-day regular-season request, up to 100 games."""
    local_day = now.astimezone(ET).date()
    season = local_day.year if local_day.month >= 7 else local_day.year - 1
    dates = [(local_day + timedelta(days=offset)).isoformat() for offset in range(3)]
    params = [("seasons[]", str(season)), ("season_type", "regular"), ("per_page", "100")]
    params += [("dates[]", day) for day in dates]
    page = fetch_json(BDL_URL + "?" + urllib.parse.urlencode(params), key=key)
    rows = page.get("data")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or row.get("date") not in dates for row in rows
    ):
        raise ValueError("STOP: BDL returned malformed or out-of-range NBA fixtures")
    fixtures = nba_fixtures(
        page, now=now, season=season, regular_season_query_verified=True,
    )
    return fixtures, len(rows)


def kalshi_events() -> tuple[dict[str, dict], int]:
    collected: dict[str, dict] = {}
    cursor = ""
    for page in range(1, MAX_EVENT_PAGES + 1):
        params = {"series_ticker": SERIES, "status": "open", "limit": 200,
                  "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        response = fetch_json(KALSHI_URL + "/events?" + urllib.parse.urlencode(params))
        rows = response.get("events")
        if not isinstance(rows, list):
            raise ValueError("Kalshi event list malformed")
        for event in rows:
            if not isinstance(event, dict) or event.get("series_ticker") != SERIES:
                raise ValueError("Kalshi event series mismatch")
            ticker = event.get("event_ticker")
            if not isinstance(ticker, str) or ticker in collected:
                raise ValueError("Kalshi event missing/duplicate ticker")
            collected[ticker] = event
        cursor = response.get("cursor") or ""
        if not cursor:
            return collected, page
    raise ValueError("STOP: NBA Kalshi pagination incomplete within request cap")


def load_history() -> list:
    games = load_final_games_csv(HISTORY_CSV)
    nba = [game for game in games if game.sport == "NBA"]
    counts = {season: sum(game.season == season for game in nba) for season in (2024, 2025)}
    if len(games) != 2460 or counts != {2024: 1230, 2025: 1230}:
        raise ValueError("STOP: NBA verified history coverage mismatch")
    return nba


def season_sensitivity_report(
    fixture: ShadowFixture, history: list, *,
    combined_home_probability: float,
) -> dict:
    """Descriptive, non-certified disagreement between separate season Elo fits.

    Different seasons start independently at the Elo prior. Their difference
    is not a confidence interval, a calibrated uncertainty estimate, or
    proof of any executable market edge. No BUY authority.
    """
    season_probs: dict[str, float] = {}
    for season in (2024, 2025):
        one_season = [
            game for game in history
            if game.sport == "NBA" and game.season == season
        ]
        if len(one_season) != 1230:
            raise ValueError("STOP: incomplete NBA season sensitivity history")
        projection = predict_future(
            one_season, sport="NBA",
            game_date=fixture.kickoff.astimezone(ET).date(),
            home_team=fixture.home_team, away_team=fixture.away_team,
            game_id=fixture.game_id, season=fixture.season,
        )
        if projection.model_status != MODEL_STATUS or projection.home_won is not None:
            raise ValueError("STOP: invalid NBA single-season experimental forecast")
        season_probs[str(season)] = projection.home_win_probability
    raw_difference_pp = 100 * abs(
        season_probs["2025"] - season_probs["2024"]
    )
    return {
        "home_probability_2024_only": round(season_probs["2024"], 6),
        "home_probability_2025_only": round(season_probs["2025"], 6),
        "home_probability_both_seasons": round(combined_home_probability, 6),
        "single_season_difference_pp": round(raw_difference_pp, 2),
        "status": "DESCRIPTIVE_UNVALIDATED",
        "current_2026_season_results_included": False,
        "net_trading_edge_certified": False,
    }


def evaluate_fixture(fixture: ShadowFixture, event: dict, history: list) -> dict:
    ticker = event.get("event_ticker")
    expected = {f"{ticker}-{fixture.away_team}", f"{ticker}-{fixture.home_team}"}
    markets = event.get("markets")
    if (event.get("series_ticker") != SERIES or not isinstance(markets, list)
            or len(markets) != 2
            or {m.get("ticker") for m in markets if isinstance(m, dict)} != expected):
        raise ValueError("NBA event does not contain both exact team markets")
    prediction = predict_future(
        history, sport="NBA", game_date=fixture.kickoff.astimezone(ET).date(),
        home_team=fixture.home_team, away_team=fixture.away_team,
        game_id=fixture.game_id, season=fixture.season,
    )
    if prediction.model_status != MODEL_STATUS or prediction.home_won is not None:
        raise ValueError("NBA forecast unexpectedly certified or postgame")
    sensitivity = season_sensitivity_report(
        fixture, history, combined_home_probability=prediction.home_win_probability,
    )
    positions = []
    for raw in markets:
        if raw.get("event_ticker") != ticker:
            raise ValueError("NBA market-event association mismatch")
        book = fetch_json(KALSHI_URL + "/markets/" + urllib.parse.quote(raw["ticker"], safe="")
                          + "/orderbook?depth=5")
        observed = datetime.now(UTC)
        if observed >= fixture.kickoff:
            raise ValueError("NBA game has already started")
        market = attach_fees(
            normalize_kalshi(raw, book, observed.isoformat(), event=event), observed,
        )
        for side in (Side.YES, Side.NO):
            play = evaluate_shadow(market, fixture, prediction, side=side, now=observed)
            if play is None or play.suggested_action.value == "BUY":
                raise ValueError("NBA shadow market/evidence rejected; BUY prohibited")
            ask = market.yes_ask if side == Side.YES else market.no_ask
            depth = market.executable_depth.get(side, ())
            qty = sum(q for price, q in depth if ask is not None and price == ask)
            selected = raw["ticker"].rsplit("-", 1)[-1]
            outcome = selected if side == Side.YES else (
                fixture.away_team if selected == fixture.home_team else fixture.home_team
            )
            positions.append({
                "ticker": raw["ticker"], "side": side.value, "outcome": outcome,
                "action": play.suggested_action.value, "ask": ask,
                "available_at_ask": qty, "gross_edge_pp": play.edge_points,
                "fee_status": market.mechanics.fee_status,
                "failed_gates": list(play.verdict.failed_gates),
            })
    return {
        "event_ticker": ticker, "game_id": fixture.game_id,
        "matchup": f"{fixture.away_team}@{fixture.home_team}",
        "start_utc": fixture.kickoff.isoformat(),
        "raw_elo_home_probability": round(prediction.home_win_probability, 6),
        "model_season_sensitivity": sensitivity,
        "positions": positions,
    }


def run_once(*, key: str, max_games: int) -> dict:
    if type(max_games) is not int or not 1 <= max_games <= MAX_GAMES_PER_CYCLE:
        raise ValueError("NBA scanner max-games exceeds bounded pilot cap")
    now = datetime.now(UTC)
    previous = past_screenings(RESEARCH_OUTPUT, now=now)
    history = load_history()  # Fail before consuming provider requests if history is invalid.
    fixtures, bdl_count = bdl_fixtures(key=key, now=now)
    events, pages = kalshi_events()
    matches = matching_event_candidates(
        fixtures, {"events": list(events.values()), "cursor": None},
    )
    selected = rotate_slate(matches, previous, limit=max_games)
    screened, rejected = [], []
    for _, ticker, fixture in selected:
        try:
            screened.append(evaluate_fixture(fixture, events[ticker], history))
        except (ValueError, KeyError, TypeError) as exc:
            rejected.append({"event_ticker": ticker,
                             "reason": type(exc).__name__ + ": " + str(exc)[:140]})
    return {
        "mode": "RESEARCH_ONLY", "sport": "NBA", "buy_authorized": False,
        "orders_authorized": False, "publish_authorized": False,
        "alerts_authorized": False,
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "history": "2024-2025 NBA final regular seasons only; no 2026 updates",
        "model": "EXPERIMENTAL_NOT_CERTIFIED / raw Elo",
        "fees": "UNVERIFIED_UNTIL_POLICY_REVIEW",
        "bdl_rows": bdl_count, "fixture_count": len(fixtures),
        "kalshi_event_count": len(events), "kalshi_pages": pages,
        "matched_candidates": len(matches),
        "selected_event_tickers": [t for _, t, _ in selected],
        "screen_limit": max_games, "screened": screened, "rejected": rejected,
        "request_budget_max": 1 + MAX_EVENT_PAGES + 2 * max_games,
    }


def save_research_snapshot(report: dict) -> Path:
    RESEARCH_OUTPUT.mkdir(parents=True, exist_ok=True)
    target = RESEARCH_OUTPUT / ("nba-shadow-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    with target.open("x", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(report, output, indent=2, sort_keys=True)
        output.write("\n")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-games", type=int, default=2)
    parser.add_argument("--write-research-snapshot", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_games <= MAX_GAMES_PER_CYCLE:
        parser.error("NBA shadow max-games must be 1 or 2")
    key = read_provider_key()
    with guarded_run():
        report = run_once(key=key, max_games=args.max_games)
        if args.write_research_snapshot:
            print("NBA RESEARCH SNAPSHOT:", save_research_snapshot(report), flush=True)
    if args.summary_only:
        print(f"NBA SHADOW {report['captured_at_utc']}: fixtures={report['fixture_count']} "
              f"matched={report['matched_candidates']} "
              f"screened={len(report['screened'])} rejected={len(report['rejected'])}; "
              "NO BUY / ORDERS / PUBLISH / ALERTS", flush=True)
    else:
        print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except ScanSkipped as exc:
        print(f"NBA SHADOW SKIPPED: {exc}", flush=True)
