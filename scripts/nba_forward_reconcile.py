#!/usr/bin/env python3
"""NBA forward reconciliation from frozen research and verified final games.

--games-json is completely offline (requires explicit provenance attestation).
--live makes AT MOST one BALLDONTLIE games GET per UTC day, and only when an
unreconciled pregame forecast is mature. Independently locked and pre-debited
request budget; 24-hour auth/rate-denial cooldown; zero automatic retries.
No service installation, model retraining, trading, public feed, or alerts.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from parallax.nba_forward_reconciliation import (
    ReconciliationError, archived_forecasts, pending_date, reconcile_day,
    read_reconciled_ids, accumulated_summary, summary,
)
from scripts.nba_shadow_slate_research import read_provider_key

ROOT = Path.home() / "parallax-research/nba"
ARCHIVE = ROOT / "forward"
LEDGER = ARCHIVE / "reconciled"
SOURCES = ARCHIVE / "final-sources"
BUDGET_FILE = ROOT / ".nba-reconcile-budget.json"
LOCK_FILE = ROOT / ".nba-reconcile.lock"
BDL_URL = "https://api.balldontlie.io/v1/games"
COOLDOWN_HOURS = 24
MAX_DATES_IN_BUDGET = 1000


def _valid_day(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
    except ValueError:
        return False


def load_budget_state() -> dict:
    """All persisted budget state is untrusted; malformed state stops requests."""
    if not BUDGET_FILE.exists():
        return {}
    try:
        state = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("Budget not an object")
        attempts = state.get("date_last_attempt_utc", {})
        if not isinstance(attempts, dict) or len(attempts) > MAX_DATES_IN_BUDGET:
            raise ValueError("Bad date rotation history")
        for date, when in attempts.items():
            if not _valid_day(date) or not isinstance(when, str):
                raise ValueError("Invalid attempt date")
            instant = datetime.fromisoformat(when)
            if instant.utcoffset() is None:
                raise ValueError("Naive attempt")
        if "utc_day" in state:
            if not _valid_day(state["utc_day"]) or state.get("reads_today") != 1:
                raise ValueError("Invalid daily allowance")
        cooldown = state.get("cooldown_until_utc")
        if cooldown is not None:
            if not isinstance(cooldown, str) or datetime.fromisoformat(cooldown).utcoffset() is None:
                raise ValueError("Invalid denial cooldown")
        return state
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        raise ReconciliationError("NBA final-read budget corrupted; fail closed") from exc


def _save_budget(state: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".nba-reconcile-budget-", dir=ROOT)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(state, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, BUDGET_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def claim_daily_read(now: datetime, *, date: str) -> dict:
    """Persist a spent daily allowance BEFORE sending any provider request."""
    state = load_budget_state()
    if not _valid_day(date):
        raise ReconciliationError("Invalid NBA query date")
    cooldown = state.get("cooldown_until_utc")
    if cooldown and now < datetime.fromisoformat(cooldown):
        raise ReconciliationError("NBA provider-denial cooldown active")
    if state.get("utc_day") == now.date().isoformat():
        raise ReconciliationError("NBA final-read daily allowance already spent")
    checks = dict(state.get("date_last_attempt_utc", {}))
    checks[date] = now.isoformat()
    if len(checks) > MAX_DATES_IN_BUDGET:
        raise ReconciliationError("NBA date history at capacity; fail closed")
    updated = {
        "utc_day": now.date().isoformat(), "reads_today": 1,
        "last_claim_utc": now.isoformat(), "cooldown_until_utc": cooldown,
        "date_last_attempt_utc": checks,
    }
    _save_budget(updated)
    return updated


def fetch_bdl_final_date(*, date: str, key: str) -> dict:
    """Exactly one authenticated, date-filtered NBA games GET; no retries."""
    if not _valid_day(date):
        raise ReconciliationError("NBA final query date invalid")
    season = int(date[:4]) if int(date[5:7]) >= 7 else int(date[:4])-1
    query = urllib.parse.urlencode([
        ("dates[]", date), ("seasons[]", str(season)),
        ("season_type", "regular"), ("per_page", "100"),
    ])
    url = BDL_URL + "?" + query
    request = urllib.request.Request(url, headers={
        "Authorization": key, "Accept": "application/json",
        "User-Agent": "PARALLAX-NBA-Forward-Reconcile/0.1",
    })
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ReconciliationError("NBA final source not a JSON object")
    return result


def _save_source(response: dict, *, date: str, now: datetime) -> Path:
    """Private, immutable capture for audit; never includes the API credential."""
    SOURCES.mkdir(parents=True, exist_ok=True)
    name = f"nba-bdl-final-{date}-{now:%Y%m%dT%H%M%S%fZ}.json"
    target = SOURCES / name
    with target.open("x", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(response, stream, sort_keys=True, indent=2)
        stream.write("\n")
    return target


def run(*, games_json: Path | None = None, regular_query_verified: bool = False,
        live: bool = False, now: datetime | None = None) -> dict:
    if live == (games_json is not None):
        raise ValueError("Choose exactly one NBA final source: --games-json or --live")
    if not live and not regular_query_verified:
        raise ReconciliationError("NBA saved regular-season source not verified")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    ROOT.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReconciliationError("NBA reconciliation already running") from exc
        archived = archived_forecasts(ARCHIVE, now=current)
        completed = read_reconciled_ids(LEDGER)
        budget = load_budget_state() if live else {}
        attempted = {
            date: datetime.fromisoformat(when).astimezone(UTC)
            for date, when in budget.get("date_last_attempt_utc", {}).items()
        }
        selected = pending_date(
            archived, completed, now=current,
            last_attempts=attempted if live else None,
        )
        if selected is None:
            return {
                "status": "NOTHING_MATURED", "network_gets": 0,
                "new_finals": 0, "forward_metrics": accumulated_summary(LEDGER),
            }
        reads = 0
        source_receipt = None
        if live:
            credential = read_provider_key()  # If missing, fail before spending budget.
            state = claim_daily_read(current, date=selected)
            reads = 1
            try:
                source = fetch_bdl_final_date(date=selected, key=credential)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 429):
                    state["cooldown_until_utc"] = (
                        datetime.now(UTC) + timedelta(hours=COOLDOWN_HOURS)
                    ).isoformat()
                    _save_budget(state)
                raise
            # Persist the exact response for offline reproducibility.
            source_receipt = _save_source(source, date=selected, now=current)
        else:
            source = json.loads(games_json.read_text(encoding="utf-8"))
        rows, pending = reconcile_day(
            archived, source, requested_date=selected,
            reconciled_ids=completed, observed=current,
            regular_query_verified=True,
        )
        if rows:
            LEDGER.mkdir(parents=True, exist_ok=True)
        for row in rows:
            output = LEDGER / f"nba-{row['game_id'].removeprefix('bdl:')}.json"
            with output.open("x", encoding="utf-8") as file:
                os.fchmod(file.fileno(), 0o600)
                json.dump(row, file, indent=2, sort_keys=True)
                file.write("\n")
        return {
            "status": "RESEARCH_ONLY", "nba_date_et": selected,
            "network_gets": reads, "new_finals": len(rows),
            "awaiting_final": pending, "new_final_metrics": summary(rows),
            "forward_metrics": accumulated_summary(LEDGER),
            "source_receipt": str(source_receipt) if source_receipt else None,
            "buy_authorized": False, "publish_authorized": False,
            "orders_authorized": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--games-json", type=Path)
    sources.add_argument("--live", action="store_true")
    parser.add_argument("--regular-query-verified", action="store_true")
    args = parser.parse_args()
    if args.games_json is not None and not args.regular_query_verified:
        parser.error("--games-json requires --regular-query-verified")
    print(json.dumps(
        run(games_json=args.games_json, live=args.live,
            regular_query_verified=args.regular_query_verified),
        indent=2, sort_keys=True,
    ))


if __name__ == "__main__":
    main()
