#!/usr/bin/env python3
"""Bounded NHL official-result reconciliation; never trades or publishes.

Offline mode requires --scoreboard-json; optional --live uses at most one
read-only official NHL scoreboard GET per UTC day, no retries. No Kalshi,
BALLDONTLIE, CFBD, production feed, ntfy or order provider calls.
No schedule is installed by this script.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

from parallax.nhl_forward_reconciliation import (
    ReconciliationError, accumulated_summary, archived_forecasts, pending_date,
    read_reconciled_ids, reconcile_day, summary,
)
from scripts.nhl_shadow_slate_research import NHL, fetch_json

ROOT = Path.home() / "parallax-research/nhl"
ARCHIVE = ROOT / "forward"
LEDGER = ARCHIVE / "reconciled"
BUDGET_FILE = ROOT / ".nhl-reconcile-budget.json"
LOCK_FILE = ROOT / ".nhl-reconcile.lock"
COOLDOWN_HOURS = 24


def _save_state(state: dict) -> None:
    # As with shadow-scanner budget, debit before request, atomically.
    fd, tmp = tempfile.mkstemp(prefix=".nhl-reconcile-budget-", dir=ROOT)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(state, out, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, BUDGET_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def claim_single_daily_read(now: datetime) -> dict:
    if BUDGET_FILE.exists():
        try:
            previous = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
            if not isinstance(previous, dict):
                raise ValueError("not an object")
            cooldown = previous.get("cooldown_until_utc")
            if cooldown and now < datetime.fromisoformat(cooldown):
                raise ReconciliationError("Provider cooldown active")
            if previous.get("utc_day") == now.date().isoformat():
                raise ReconciliationError("Daily official NHL scoreboard read already spent")
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            raise ReconciliationError("Reconciliation budget corrupt; fail closed") from exc
    else:
        previous = {}
    updated = {
        "utc_day": now.date().isoformat(),
        "reads_today": 1,
        "last_claim_utc": now.isoformat(),
        "cooldown_until_utc": previous.get("cooldown_until_utc"),
    }
    _save_state(updated)
    return updated


def run(*, scoreboard_json: Path | None = None, live: bool = False,
        now: datetime | None = None) -> dict:
    if live == (scoreboard_json is not None):
        raise ValueError("Choose exactly one scoreboard source")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    ROOT.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReconciliationError("Reconciler already running") from exc
        frozen = archived_forecasts(ARCHIVE, now=current)
        completed = read_reconciled_ids(LEDGER)
        game_date = pending_date(frozen, completed, now=current)
        if game_date is None:
            return {
                "status": "NOTHING_MATURED",
                "scoreboard_gets": 0, "new_finals": 0,
                "forward_metrics": accumulated_summary(LEDGER),
            }
        if scoreboard_json is not None:
            score = json.loads(scoreboard_json.read_text(encoding="utf-8"))
            reads = 0
        else:
            state = claim_single_daily_read(current)
            reads = 1
            try:
                score = fetch_json(f"{NHL}/score/{game_date}")
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429):
                    state["cooldown_until_utc"] = (
                        datetime.now(UTC) + timedelta(hours=COOLDOWN_HOURS)
                    ).isoformat()
                    _save_state(state)
                raise
        rows, awaiting = reconcile_day(
            frozen, score, requested_date=game_date,
            reconciled_ids=completed, captured_at=current,
        )
        if rows:
            LEDGER.mkdir(parents=True, exist_ok=True)
        for row in rows:
            filename = f"nhl-{row['game_id'].removeprefix('nhl:')}.json"
            target = LEDGER / filename
            # File must never be overwritten, including by parallel processes
            # that might not share the lock. Existing completed games skip
            # future score requests but do not change archived outcomes.
            with target.open("x", encoding="utf-8") as stream:
                json.dump(row, stream, indent=2, sort_keys=True)
                stream.write("\n")
        return {
            "status": "RESEARCH_ONLY",
            "scoreboard_date_et": game_date,
            "scoreboard_gets": reads,
            "new_finals": len(rows),
            "awaiting_final": awaiting,
            "new_final_metrics": summary(rows),
            "forward_metrics": accumulated_summary(LEDGER),
            "buy_authorized": False,
            "publish_authorized": False,
            "orders_authorized": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--scoreboard-json", type=Path)
    sources.add_argument("--live", action="store_true")
    args = parser.parse_args()
    result = run(scoreboard_json=args.scoreboard_json, live=args.live)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
