#!/usr/bin/env python3
"""Isolated, read-only NBA/NHL quote evidence collection runner."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from datetime import date
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parallax.league_quote_snapshots import LeagueQuoteSnapshotStore
from parallax.nba_nhl import fetch_nba_fixtures, fetch_nhl_fixtures, scan_league
from parallax.pmus_acquisition import PMUSAcquisition
from parallax.sources import KalshiPublicClient

NBA_TERMS_URL = "https://www.balldontlie.io/terms.html"


def authorization_state(sport: str, authorization: dict | None) -> tuple[bool, dict]:
    """Validate secret-free, operator-recorded account/terms authorization."""
    row = authorization.get(sport, {}) if isinstance(authorization, dict) else {}
    if not isinstance(row, dict):
        row = {}
    if sport == "NBA":
        reviewed = row.get("terms_reviewed_at")
        verified = row.get("account_plan_verified_at")
        ok = (
            row.get("provider") == "BALLDONTLIE"
            and isinstance(row.get("api_plan"), str) and bool(row["api_plan"].strip())
            and row.get("nba_games_access") is True
            and row.get("account_plan_verified") is True
            and row.get("terms_reviewed") is True
            and row.get("terms_url") == NBA_TERMS_URL
            and _recent_date(reviewed, max_age_days=365)
            and _recent_date(verified, max_age_days=90)
        )
        return ok, {"state": "AUTHORIZED" if ok else "UNVERIFIED",
                    "provider": "BALLDONTLIE", "terms_reviewed_at": reviewed if ok else None,
                    "account_plan_verified_at": verified if ok else None}
    ref = row.get("written_authorization_ref")
    ok = (
        row.get("provider") == "NHL"
        and row.get("automated_schedule_access") is True
        and isinstance(ref, str) and bool(ref.strip())
        and _recent_date(row.get("verified_at"), max_age_days=365)
    )
    return ok, {"state": "AUTHORIZED" if ok else "UNVERIFIED", "provider": "NHL",
                "written_authorization_ref": ref.strip() if ok else None,
                "verified_at": row.get("verified_at") if ok else None}


def _recent_date(value: object, *, max_age_days: int) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    age = (date.today() - parsed).days
    return 0 <= age <= max_age_days


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
                 market_fetcher: Callable | None = None,
                 leagues: tuple[str, ...] | list[str] = ("NBA", "NHL"),
                 authorization: dict | None = None) -> dict:
    now = now or datetime.now(UTC)
    fixture_fetchers = fixture_fetchers or {
        "NBA": lambda: fetch_nba_fixtures(now=now),
        "NHL": lambda: fetch_nhl_fixtures(now=now),
    }
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    store = LeagueQuoteSnapshotStore(evidence_dir)
    selected = tuple(dict.fromkeys(str(s).upper() for s in leagues))
    if not selected or any(sport not in {"NBA", "NHL"} for sport in selected):
        raise ValueError("leagues must select NBA and/or NHL")
    health = {"schema_version": 2, "updated_at": now.isoformat(), "read_only": True,
              "orders": 0, "alerts": 0, "published": 0, "leagues": {}, "exit_code": 0}
    previous = {}
    try:
        previous = json.loads(health_path.read_text(encoding="utf-8")).get("leagues", {})
    except (OSError, ValueError, AttributeError):
        pass
    acquisition = PMUSAcquisition("LEAGUE_EVIDENCE", state_path=state_dir / "pmus_acquisition.sqlite")
    failed = False
    for sport in ("NBA", "NHL"):
        old = previous.get(sport, {})
        row = {"last_attempt": now.isoformat(), "last_success": old.get("last_success"),
               "fixtures_seen": 0, "matched_contracts": 0, "snapshots_written": 0,
               "source_failures": [], "stale_snapshots": 0, "empty_coverage_reason": None,
               "status": "NOT_SELECTED"}
        authorized, auth_record = authorization_state(sport, authorization)
        row["authorization"] = auth_record
        if sport not in selected:
            health["leagues"][sport] = row
            continue
        if not authorized:
            row.update(status="AUTHORIZATION_BLOCKED", empty_coverage_reason="source_authorization_unverified")
            failed = True
            health["leagues"][sport] = row
            continue
        row["status"] = "RUNNING"
        clients = None
        try:
            fixtures = fixture_fetchers[sport]()
            row["fixtures_seen"] = len(fixtures)
            if not fixtures:
                row["status"] = "EMPTY"
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
                if cov.get("error_type"):
                    row["source_failures"].append({"source": source, "reason": cov["error_type"]})
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
            if int(stats.get("failures", 0) or 0):
                row["source_failures"].append({"source": "quote_snapshot_store",
                    "reason": "invalid_or_unavailable_executable_quote",
                    "count": int(stats["failures"])})
            discovered = sum(int(source.get("discovered", 0) or 0) for source in source_health.values())
            quote_failures = int(stats.get("failures", 0))
            acquisition_failures = any(
                source.get("coverage", {}).get("error_type") for source in source_health.values()
            ) or any(int(detail.get("book_failures", 0) or 0) > 0 for detail in result["venues"].values())
            if row["snapshots_written"] > 0 and row["stale_snapshots"] == 0 and not acquisition_failures and not quote_failures:
                row["last_success"] = now.isoformat()
                row["empty_coverage_reason"] = None
                row["status"] = "SUCCESS"
            elif row["matched_contracts"] == 0:
                row["empty_coverage_reason"] = "no_markets" if discovered == 0 else "no_supported_market_mapping"
                if discovered == 0 and not acquisition_failures:
                    row["status"] = "EMPTY"
                else:
                    row["status"] = "FAILED"
            elif row["stale_snapshots"]:
                row["empty_coverage_reason"] = "quotes_stale"
                row["status"] = "FAILED"
            else:
                row["empty_coverage_reason"] = "invalid_or_unavailable_executable_quotes"
                row["status"] = "FAILED"
            if acquisition_failures or quote_failures or row["status"] == "FAILED":
                failed = True
        except Exception as exc:
            row["source_failures"].append({"source": "collector", "reason": type(exc).__name__})
            row["empty_coverage_reason"] = "collection_failed"
            row["status"] = "FAILED"
            failed = True
        finally:
            if clients:
                for client in clients:
                    if client is not None and hasattr(client, "close"):
                        client.close()
        health["leagues"][sport] = row
    health["exit_code"] = 1 if failed else 0
    _atomic_json(health_path, health)
    return health


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=Path.home() / ".local/share/parallax/league-evidence")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/parallax/league-evidence")
    parser.add_argument("--health-path", type=Path, default=Path.home() / ".local/state/parallax/league-evidence/health.json")
    parser.add_argument("--log-path", type=Path, default=Path.home() / ".local/state/parallax/league-evidence/collector.log")
    parser.add_argument("--leagues", nargs="+", choices=("NBA", "NHL"), default=("NBA", "NHL"))
    parser.add_argument("--authorization-file", type=Path,
        default=Path.home() / ".config/parallax/league-evidence-authorization.json")
    args = parser.parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        try:
            authorization = json.loads(args.authorization_file.read_text(encoding="utf-8"))
            if not isinstance(authorization, dict):
                raise ValueError("authorization root must be an object")
        except FileNotFoundError:
            authorization = {}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"authorization file invalid: {type(exc).__name__}", file=sys.stderr)
            return 2
        args.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(args.log_path.parent, 0o700)
        import logging
        from logging.handlers import RotatingFileHandler
        handler = RotatingFileHandler(args.log_path, maxBytes=2_000_000, backupCount=5)
        os.chmod(args.log_path, 0o600)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger = logging.getLogger("league_evidence")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        lock_dir = args.state_dir
        lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(lock_dir, 0o700)
        lock_path = lock_dir / ".collector.lock"
        with lock_path.open("a") as lock:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logger.warning("another collector run holds the singleton lock")
                return 0
            health = collect_once(evidence_dir=args.evidence_dir, state_dir=args.state_dir,
                                  health_path=args.health_path, leagues=args.leagues,
                                  authorization=authorization)
            logger.info("collector completed: %s", ",".join(
                f"{sport}={health['leagues'][sport]['status']}"
                for sport in args.leagues))
        return int(health["exit_code"])
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
