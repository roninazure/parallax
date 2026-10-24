#!/usr/bin/env python3
"""Offline NBA forward reconciliation from locally saved verified BDL games JSON.

No provider GETs, API keys, scheduler registration, orders, public feed or alerts.
Use --regular-query-verified ONLY for a saved response actually requested with
season_type=regular, an exact date filter, and complete cursor pagination.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from parallax.nba_forward_reconciliation import (
    ReconciliationError, archived_forecasts, pending_date, reconcile_day,
    read_reconciled_ids, accumulated_summary, summary,
)

ROOT = Path.home() / "parallax-research/nba"
ARCHIVE = ROOT / "forward"
LEDGER = ARCHIVE / "reconciled"
LOCK_FILE = ROOT / ".nba-reconcile.lock"


def run(*, games_json: Path, regular_query_verified: bool, now: datetime | None = None) -> dict:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if not regular_query_verified:
        raise ReconciliationError("NBA saved regular-season source not verified")
    ROOT.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReconciliationError("NBA reconciliation already running") from exc
        archived = archived_forecasts(ARCHIVE, now=current)
        completed = read_reconciled_ids(LEDGER)
        selected = pending_date(archived, completed, now=current)
        if selected is None:
            return {
                "status": "NOTHING_MATURED", "network_gets": 0,
                "new_finals": 0, "forward_metrics": accumulated_summary(LEDGER),
            }
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
            "network_gets": 0, "new_finals": len(rows),
            "awaiting_final": pending, "new_final_metrics": summary(rows),
            "forward_metrics": accumulated_summary(LEDGER),
            "buy_authorized": False, "publish_authorized": False,
            "orders_authorized": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games-json", type=Path, required=True)
    parser.add_argument("--regular-query-verified", action="store_true", required=True)
    args = parser.parse_args()
    print(json.dumps(
        run(games_json=args.games_json, regular_query_verified=args.regular_query_verified),
        indent=2, sort_keys=True,
    ))


if __name__ == "__main__":
    main()
