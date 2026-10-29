#!/usr/bin/env python3
"""Isolated, read-only NBA/NHL quote evidence collection runner."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parallax.league_quote_snapshots import LeagueQuoteSnapshotStore
from parallax.nba_nhl import fetch_nba_fixtures, fetch_nhl_fixtures, scan_league
from parallax.pmus_acquisition import PMUSAcquisition
from parallax.sources import KalshiPublicClient


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".health-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def collect_once(*, evidence_dir: Path, state_dir: Path, health_path: Path,
                 now: datetime | None = None,
                 fixture_fetchers: dict[str, Callable] | None = None,
                 market_fetcher: Callable | None = None) -> dict:
    now = now or datetime.now(UTC)
    fixture_fetchers = fixture_fetchers or {
        "NBA": lambda: fetch_nba_fixtures(now=now),
        "NHL": lambda: fetch_nhl_fixtures(now=now),
    }
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    store = LeagueQuoteSnapshotStore(evidence_dir)
    health = {"schema_version": 1, "updated_at": now.isoformat(), "read_only": True,
              "orders": 0, "alerts": 0, "published": 0, "leagues": {}}
    previous = {}
    try:
        previous = json.loads(health_path.read_text(encoding="utf-8")).get("leagues", {})
    except (OSError, ValueError, AttributeError):
        pass
    acquisition = PMUSAcquisition("LEAGUE_EVIDENCE", state_path=state_dir / "pmus_acquisition.sqlite")
    for sport in ("NBA", "NHL"):
        old = previous.get(sport, {})
        row = {"last_attempt": now.isoformat(), "last_success": old.get("last_success"),
               "fixtures_seen": 0, "matched_contracts": 0, "snapshots_written": 0,
               "source_failures": [], "stale_snapshots": 0, "empty_coverage_reason": None}
        clients = None
        try:
            fixtures = fixture_fetchers[sport]()
            row["fixtures_seen"] = len(fixtures)
            if not fixtures:
                row["empty_coverage_reason"] = "empty_schedule"
                health["leagues"][sport] = row
                continue
            if market_fetcher is None:
                from scripts._league_live_scan import _live_markets
                markets = _live_markets(sport, now, acquisition, fixtures)
            else:
                markets = market_fetcher(sport, now, acquisition, fixtures)
            krows, prows, source_health, kclient, pclient = markets
            clients = (kclient, pclient)
            for source, detail in source_health.items():
                cov = detail.get("coverage", {})
                if cov.get("state") == "PARTIAL" or cov.get("error_type"):
                    row["source_failures"].append({"source": source, "reason": cov.get("error_type", "partial coverage")})
            result = scan_league(sport, fixtures, [], krows, prows,
                kalshi_client=kclient, pmus_client=pclient, pmus_acquisition=acquisition,
                alert_dispatcher=None, model_artifact=None,
                model_artifact_error="collector does not evaluate models",
                quote_snapshot_store=store, now=now)
            stats = result["quote_snapshots"]
            row["matched_contracts"] = stats["matched_contracts"]
            row["snapshots_written"] = stats["written"]
            row["stale_snapshots"] = stats["stale"]
            row["deduplicated_snapshots"] = stats["deduplicated"]
            row["source_failures"].extend(
                {"source": source, "reason": "book acquisition failures", "count": detail["book_failures"]}
                for source, detail in result["venues"].items() if detail["book_failures"]
            )
            if row["snapshots_written"] > 0 and row["stale_snapshots"] == 0:
                row["last_success"] = now.isoformat()
                row["empty_coverage_reason"] = None
            elif not fixtures:
                row["empty_coverage_reason"] = "empty_schedule"
            elif row["matched_contracts"] == 0:
                row["empty_coverage_reason"] = "no_supported_market_mapping"
            elif row["snapshots_written"] == 0:
                row["empty_coverage_reason"] = "no_fresh_executable_quotes"
            elif row["stale_snapshots"]:
                row["empty_coverage_reason"] = "quotes_stale"
        except Exception as exc:
            row["source_failures"].append({"source": "collector", "reason": type(exc).__name__})
            row["empty_coverage_reason"] = "collection_failed"
        finally:
            if clients:
                for client in clients:
                    if client is not None and hasattr(client, "close"):
                        client.close()
        health["leagues"][sport] = row
    _atomic_json(health_path, health)
    return health


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=Path.home() / ".local/share/parallax/league-evidence")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/parallax/league-evidence")
    parser.add_argument("--health-path", type=Path, default=Path.home() / ".local/state/parallax/league-evidence/health.json")
    parser.add_argument("--log-path", type=Path, default=Path.home() / ".local/state/parallax/league-evidence/collector.log")
    args = parser.parse_args()
    if not os.environ.get("PARALLAX_NHL_EVIDENCE_AUTHORIZATION_REF", "").strip():
        print("blocked: PARALLAX_NHL_EVIDENCE_AUTHORIZATION_REF is required before automated NHL collection", file=sys.stderr)
        return 2
    args.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.log_path.parent, 0o700)
    import logging
    from logging.handlers import RotatingFileHandler
    handler = RotatingFileHandler(args.log_path, maxBytes=2_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("league_evidence")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    lock_dir = args.state_dir
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(lock_dir, 0o700)
    with (lock_dir / ".collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.warning("another collector run holds the singleton lock")
            return 0
        health = collect_once(evidence_dir=args.evidence_dir, state_dir=args.state_dir,
                              health_path=args.health_path)
        logger.info("collector completed NBA=%s NHL=%s", health["leagues"]["NBA"]["snapshots_written"],
                    health["leagues"]["NHL"]["snapshots_written"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
