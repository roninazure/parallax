#!/usr/bin/env python3
"""One-shot isolated CFB live-data probe. Does not publish or deliver alerts."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from scripts.parallax_unattended import load_runtime_env
    from scripts import cfb_live_scan as scan

    runtime = Path.home() / "Library/Application Support/SwarmEdge/config/runtime.env"
    if not runtime.is_file():
        raise SystemExit("BLOCKED: runtime configuration unavailable")
    for key, value in load_runtime_env(runtime).items():
        os.environ[key] = value

    with tempfile.TemporaryDirectory(prefix="parallax-cfb-probe-") as tmp:
        os.chdir(tmp)
        scan.PROSPECTIVE_DB = Path(tmp) / "prospective.sqlite"
        # No notification delivery even when a real, eligible BUY is found.
        scan._dispatch_eligible_buy_alert = lambda *a, **kw: None
        output = scan._scan(alert_dispatcher=object())

    rows = output.get("rows") or []
    result = {
        "read_only": output.get("read_only"),
        "orders": output.get("orders"),
        "alerts": output.get("alerts"),
        "cfbd_calls": output.get("cfbd_calls"),
        "prospective_captured": output.get("prospective_captured"),
        "venues": {},
        "verdicts": dict(Counter(row.get("verdict") for row in rows)),
        "fees": dict(Counter(row.get("fee_status") for row in rows)),
        "failed_gates": dict(Counter(gate for row in rows for gate in row.get("failed_gates", []))),
        "eligible_buy_rows": sum(1 for row in rows if row.get("publication_eligible")),
        "samples": [
            {k: row.get(k) for k in (
                "venue", "matchup", "selected_team", "cfb_v1_probability",
                "executable_price", "fee_status", "raw_edge", "verdict",
                "publication_eligible", "failed_gates"
            )}
            for row in rows[:8]
        ],
    }
    for venue, data in output.get("venues", {}).items():
        result["venues"][venue] = {
            k: data.get(k) for k in (
                "discovered", "mapped", "executable_book", "model_probability",
                "fully_scored_sides", "BUY", "WATCH", "PASS", "status_counts", "coverage"
            )
        }
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if output.get("orders") != 0 or output.get("alerts") != 0:
        raise SystemExit("BLOCKED: probe isolation invariant violated")
    if not rows:
        raise SystemExit("INCOMPLETE: no scored CFB rows; inspect venue coverage")

if __name__ == "__main__":
    main()
