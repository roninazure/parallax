"""Shared fail-closed BALLDONTLIE request reservation for one Mac mini host.

The provider advertises five requests/minute on Free. Reserve at most
FOUR requests per rolling 60 seconds across processes using this database.
No sleep, retry, or deferred request. All NBA callers must use this module;
it cannot constrain unrelated external clients/accounts or other machines.
"""
from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path

REQUEST_LIMIT = 4
WINDOW_SECONDS = 60.0
DEFAULT_LEDGER = Path.home() / ".local/state/parallax/balldontlie-request-budget.sqlite3"


class RequestBudgetExceeded(RuntimeError):
    pass


def reserve_bdl_request(path: Path = DEFAULT_LEDGER, *, now: float | None = None) -> None:
    """Record a request attempt before network IO, or reject without a call.

    Deliberately count failed HTTP calls as requests. Never refund a slot.
    """
    if now is None:
        now = time.time()
    if not isinstance(now, (int, float)) or not (0 < now < float("inf")):
        raise RequestBudgetExceeded("invalid rate-limit clock")
    path = Path(path)
    if path.exists() and path.is_symlink():
        raise RequestBudgetExceeded("request ledger symlink rejected")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RequestBudgetExceeded("request ledger directory ownership/mode unsafe")
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise RequestBudgetExceeded("request ledger file ownership/mode unsafe")
    try:
        with sqlite3.connect(path, timeout=5, isolation_level=None) as db:
            # Explicitly deny any work if an existing schema is unreadable.
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS requests (timestamp REAL NOT NULL)")
            rows = [float(row[0]) for row in db.execute(
                "SELECT timestamp FROM requests"
            )]
            if any(value > now or value <= 0 for value in rows):
                raise RequestBudgetExceeded("clock rollback or invalid rate-limit ledger")
            # Do not include the 60-second-old boundary in the rolling window.
            recent = [value for value in rows if value > now - WINDOW_SECONDS]
            if len(recent) >= REQUEST_LIMIT:
                raise RequestBudgetExceeded("BALLDONTLIE shared request budget exhausted")
            db.execute("DELETE FROM requests WHERE timestamp <= ?", (now - WINDOW_SECONDS,))
            db.execute("INSERT INTO requests (timestamp) VALUES (?)", (now,))
            db.execute("COMMIT")
    except sqlite3.Error as exc:
        raise RequestBudgetExceeded("BALLDONTLIE shared request budget unavailable") from exc
    # SQLite creates journal files under the private directory; harden main db.
    if path.exists():
        os.chmod(path, 0o600)
