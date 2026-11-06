#!/usr/bin/env python3
"""One-shot NFL sportsbook evidence capture with persistent fail-closed throttling.

Research only: never publishes BUY signals, changes forecasts, or calls the
production scanner. The scheduler invokes this as an optional isolated lane.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
import time
import subprocess
from getpass import getuser
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parallax.nfl import NFLGame, fetch_games, parse_games
from sports.nfl_evidence import match_nfl_evidence
from sports.odds_provider import OddsResponse
from sports.nfl_odds_v4 import fetch_nfl_v4_once

MINIMUM_CAPTURE_INTERVAL_SECONDS = 3600
MINIMUM_PAID_CAPTURE_INTERVAL_SECONDS = 24 * 3600
MAX_UTC_CALENDAR_MONTH_RESEARCH_ATTEMPTS = 8
FREE_USAGE_ENDPOINT = "https://api.the-odds-api.com/v4/sports/"
KEYCHAIN_SERVICE = "swarmaxis-the-odds-api"


def capture_games(response: OddsResponse, games: list[NFLGame], *, observed_at: datetime) -> dict:
    """Pure offline conversion; no network, notification, or publication."""
    if observed_at.tzinfo is None:
        raise ValueError("capture requires timezone-aware observed_at")
    matched = []
    for event in response.events:
        evidence = match_nfl_evidence(event, games, observed_at=observed_at)
        if evidence is not None:
            matched.append(asdict(evidence))
    return {
        "schema_version": "nfl-sportsbook-research-v1",
        "research_only": True,
        "buy_publication_eligible": False,
        "captured_at_utc": observed_at.astimezone(UTC).isoformat(),
        "events_seen": len(response.events),
        "events_accepted": len(matched),
        "evidence": matched,
        "limitations": [
            "Captured timestamps are retrieval times, not guaranteed provider quote observation times.",
            "Provider quote ages use market-level last_update timestamps.",
            "Consensus is market evidence, not independently validated model alpha.",
            "No customer BUY publication, historical execution claims, or live forecast modification.",
        ],
    }


def capture(response: OddsResponse, games_csv: str, *, observed_at: datetime) -> dict:
    """Compatibility wrapper for deterministic offline CSV-based tests."""
    return capture_games(response, parse_games(games_csv), observed_at=observed_at)


def quota_headroom_verified(
    state: dict | None, *, reserve: int, now: datetime,
    max_age_seconds: int = 7200,
) -> bool:
    """Require positive headroom from a recent, timezone-aware quota snapshot."""
    if (
        not isinstance(state, dict)
        or reserve < 1
        or max_age_seconds < 1
        or now.tzinfo is None
    ):
        return False
    try:
        remaining = int(state["remaining"])
        captured_at = datetime.fromisoformat(
            str(state["captured_at_utc"]).replace("Z", "+00:00")
        )
        if captured_at.tzinfo is None:
            return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    age = (now.astimezone(UTC) - captured_at.astimezone(UTC)).total_seconds()
    if age < 0 or age > max_age_seconds:
        return False
    return remaining > reserve


def resolve_odds_api_key() -> str | None:
    """Prefer the macOS System Keychain for LaunchDaemons; preserve login fallback.

    Never print secrets or keychain errors. The system item must be provisioned
    separately with an appropriate access policy before enabling research.
    """
    provided = os.environ.get("THE_ODDS_API_KEY", "").strip()
    if provided:
        return provided
    if sys.platform != "darwin":
        return None
    command = ["/usr/bin/security", "find-generic-password", "-w",
               "-s", KEYCHAIN_SERVICE, "-a", getuser()]
    # LaunchDaemons have a different keychain search context than login shells.
    # System.keychain works without an interactive login once explicitly allowed.
    for keychain in ("/Library/Keychains/System.keychain", None):
        try:
            result = subprocess.run(
                command + ([keychain] if keychain else []),
                text=True, capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def fetch_free_usage_snapshot(*, api_key: str, now: datetime) -> dict:
    """Read account credits with the zero-cost sports endpoint.

    Reject absent/invalid headers, transport failures, or any unexpected
    nonzero cost; never include the credential-bearing URL in errors.
    """
    if not api_key or now.tzinfo is None:
        raise ValueError("valid API credential and aware UTC clock required")
    request = Request(
        FREE_USAGE_ENDPOINT + "?" + urlencode({"apiKey": api_key}),
        headers={"Accept": "application/json", "User-Agent": "parallax-research/1"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise ValueError("unexpected free quota status")
            remaining = int(response.headers["x-requests-remaining"])
            used = int(response.headers["x-requests-used"])
            last = int(response.headers["x-requests-last"])
            if remaining < 0 or used < 0 or last != 0:
                raise ValueError("invalid zero-cost quota response")
            response.read()
    except Exception:
        raise RuntimeError("free quota check unavailable; refusing paid odds request") from None
    return {
        "captured_at_utc": now.astimezone(UTC).isoformat(),
        "limit": used + remaining,
        "used": used,
        "remaining": remaining,
        "reset_epoch": None,
        "source": "ZERO_COST_SPORTS_ENDPOINT",
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        os.chmod(handle.name, 0o600)
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _read_capture_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("capture state is unreadable; refusing provider request") from exc
    if not isinstance(value, dict) or not isinstance(value.get("last_attempt_at_utc"), str):
        raise RuntimeError("capture state is malformed; refusing provider request")
    return value


def reserve_capture_attempt(
    path: Path, *, now: datetime, min_interval_seconds: int = MINIMUM_CAPTURE_INTERVAL_SECONDS
) -> dict:
    """Persist a request reservation before HTTP; survive restarts and failures."""
    if now.tzinfo is None:
        raise ValueError("capture reservation requires timezone-aware time")
    if min_interval_seconds < MINIMUM_CAPTURE_INTERVAL_SECONDS:
        raise ValueError("NFL capture interval cannot be less than one hour")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = _read_capture_state(path)
            previous = state.get("last_attempt_at_utc")
            if previous:
                try:
                    previous_at = datetime.fromisoformat(previous.replace("Z", "+00:00"))
                    if previous_at.tzinfo is None:
                        raise ValueError("naive capture state timestamp")
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("capture state timestamp is invalid; refusing provider request") from exc
                elapsed = (now.astimezone(UTC) - previous_at.astimezone(UTC)).total_seconds()
                if elapsed < max(min_interval_seconds, MINIMUM_PAID_CAPTURE_INTERVAL_SECONDS):
                    raise RuntimeError("NFL sportsbook capture throttled by persistent 24-hour paid-attempt limit")
            month = now.astimezone(UTC).strftime("%Y-%m")
            prior_month = state.get("attempt_month_utc")
            prior_count = state.get("attempt_count_month_utc")
            if (prior_month is None) != (prior_count is None):
                raise RuntimeError("monthly capture budget state is incomplete; refusing provider request")
            if prior_month is not None:
                if (
                    not isinstance(prior_month, str)
                    or len(prior_month) != 7
                    or prior_month[4] != "-"
                    or not prior_month[:4].isdigit()
                    or not prior_month[5:].isdigit()
                    or not 1 <= int(prior_month[5:]) <= 12
                    or prior_month > month
                    or isinstance(prior_count, bool)
                    or not isinstance(prior_count, int)
                    or not 0 <= prior_count <= MAX_UTC_CALENDAR_MONTH_RESEARCH_ATTEMPTS
                ):
                    raise RuntimeError("monthly capture budget state is invalid; refusing provider request")
            elif previous:
                # An existing reservation without a reliable monthly count
                # cannot prove remaining research budget.
                raise RuntimeError("legacy capture state has no monthly budget; refusing provider request")
            count = prior_count if prior_month == month else 0
            if count >= MAX_UTC_CALENDAR_MONTH_RESEARCH_ATTEMPTS:
                raise RuntimeError(f"NFL research monthly {MAX_UTC_CALENDAR_MONTH_RESEARCH_ATTEMPTS}-attempt safety budget exhausted")
            reserved = {
                **state,
                "last_attempt_at_utc": now.astimezone(UTC).isoformat(),
                "attempt_month_utc": month,
                "attempt_count_month_utc": count + 1,
                "status": "REQUEST_RESERVED",
            }
            _atomic_write_json(path, reserved)
            return reserved
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def update_capture_state(path: Path, **updates: object) -> None:
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = _read_capture_state(path)
            _atomic_write_json(path, {**state, **updates})
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def eligible_capture_games(
    games: list[NFLGame], *, now: datetime, min_lead_minutes: int = 60,
    max_lookback_hours: int = 24,
) -> list[NFLGame]:
    """Only collect quotes that can qualify for the forward evaluation window."""
    if now.tzinfo is None:
        raise ValueError("evaluation window requires timezone-aware time")
    if min_lead_minutes < 0 or max_lookback_hours < 1:
        raise ValueError("invalid capture evaluation window")
    earliest = now.astimezone(UTC) + timedelta(minutes=min_lead_minutes)
    latest = now.astimezone(UTC) + timedelta(hours=max_lookback_hours)
    eligible = []
    for game in games:
        if game.home_score is not None or game.away_score is not None:
            continue
        try:
            kickoff = datetime.fromisoformat(game.kickoff.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            continue
        if kickoff.tzinfo is None:
            continue
        kickoff = kickoff.astimezone(UTC)
        if earliest <= kickoff <= latest:
            eligible.append(game)
    return sorted(eligible, key=lambda game: (game.kickoff, game.game_id))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    schedule = parser.add_mutually_exclusive_group(required=True)
    schedule.add_argument("--games-csv", type=Path)
    schedule.add_argument("--fetch-schedule", action="store_true",
                          help="Fetch the existing nflverse official schedule once")
    parser.add_argument("--quota-state", type=Path, default=Path("reports/sports_quota_state.json"))
    parser.add_argument("--capture-state", type=Path, default=Path("reports/nfl_sportsbook_capture_state.json"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-quota-reserve", type=int, default=10)
    parser.add_argument("--min-interval-seconds", type=int, default=MINIMUM_CAPTURE_INTERVAL_SECONDS)
    parser.add_argument("--min-lead-minutes", type=int, default=60)
    parser.add_argument("--max-lookback-hours", type=int, default=24)
    parser.add_argument("--fetch-once", action="store_true", help="Explicitly authorize one odds request")
    args = parser.parse_args()

    if not args.fetch_once:
        parser.error("no network request without --fetch-once")
    if args.min_quota_reserve < 1:
        parser.error("quota reserve must be positive")
    if args.min_interval_seconds < MINIMUM_CAPTURE_INTERVAL_SECONDS:
        parser.error("minimum capture interval cannot be less than 3600 seconds")
    if args.min_lead_minutes < 0 or args.max_lookback_hours < 1:
        parser.error("invalid capture evaluation window")

    # The production NFL parser's date-only fallback uses the host timezone.
    # This isolated child must interpret NFL schedule dates as Eastern
    # regardless of macOS host settings; never modify the live scanner.
    if hasattr(time, "tzset"):
        os.environ["TZ"] = "America/New_York"
        time.tzset()

    # Fetch the official schedule before quota checks. If no game can qualify
    # for the pregame evaluation window, do not spend an Odds API request.
    if args.fetch_schedule:
        try:
            games = fetch_games()
        except Exception as exc:
            parser.error(f"official NFL schedule fetch failed: {type(exc).__name__}")
    else:
        games = parse_games(args.games_csv.read_text(encoding="utf-8"))
    if not games:
        parser.error("empty or invalid official NFL schedule")

    now = datetime.now(UTC)
    eligible_games = eligible_capture_games(
        games, now=now, min_lead_minutes=args.min_lead_minutes,
        max_lookback_hours=args.max_lookback_hours,
    )
    if not eligible_games:
        print(json.dumps({
            "status": "SKIPPED_NO_ELIGIBLE_GAMES",
            "research_only": True,
            "buy_publication_eligible": False,
            "schedule_games": len(games),
            "eligible_games": 0,
            "min_lead_minutes": args.min_lead_minutes,
            "max_lookback_hours": args.max_lookback_hours,
        }, sort_keys=True))
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        parser.error("refusing to overwrite existing snapshot")
    api_key = resolve_odds_api_key()
    if not api_key:
        parser.error("Odds API credential unavailable in runtime or operator Keychain")
    try:
        state = fetch_free_usage_snapshot(api_key=api_key, now=now)
    except RuntimeError:
        parser.error("free quota check failed; refusing paid odds request")
    _atomic_write_json(args.quota_state, state)
    if not quota_headroom_verified(state, reserve=args.min_quota_reserve, now=now):
        parser.error("quota reserve reached: refusing paid odds request")

    try:
        reserve_capture_attempt(
            args.capture_state, now=now, min_interval_seconds=args.min_interval_seconds
        )
    except RuntimeError as exc:
        parser.error(str(exc))

    try:
        response = fetch_nfl_v4_once(api_key=api_key, now=now)
        observed_at = datetime.now(UTC)
        # A request can cross the 60-minute cutoff. Re-evaluate the official
        # fixtures at response receipt, matching the quote-age reference clock.
        eligible_at_response = eligible_capture_games(
            games, now=observed_at, min_lead_minutes=args.min_lead_minutes,
            max_lookback_hours=args.max_lookback_hours,
        )
        if response.quota.remaining is not None and response.quota.used is not None:
            _atomic_write_json(args.quota_state, {
                "captured_at_utc": observed_at.isoformat(),
                "limit": response.quota.limit,
                "used": response.quota.used,
                "remaining": response.quota.remaining,
                "reset_epoch": response.quota.reset_epoch,
                "source": "NFL_ODDS_RESPONSE",
            })
        report = capture_games(response, eligible_at_response, observed_at=observed_at)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as target:
            json.dump(report, target, indent=2, sort_keys=True)
            target.write("\n")
        update_capture_state(
            args.capture_state,
            status="SUCCESS",
            last_success_at_utc=observed_at.isoformat(),
            last_snapshot_path=str(args.output),
            last_events_seen=report["events_seen"],
            last_events_accepted=report["events_accepted"],
        )
    except Exception as exc:
        update_capture_state(
            args.capture_state,
            status="FAILED",
            last_error_type=type(exc).__name__,
        )
        # The Odds API key is sent in the provider URL query string. Never let
        # transport exceptions or request URLs reach supervisor health logs.
        raise RuntimeError(
            f"NFL sportsbook capture failed: {type(exc).__name__}"
        ) from None

    print(json.dumps({
        "status": "CAPTURED",
        "schema_version": report["schema_version"],
        "research_only": report["research_only"],
        "buy_publication_eligible": report["buy_publication_eligible"],
        "output": str(args.output),
        "events_seen": report["events_seen"],
        "events_accepted": report["events_accepted"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
