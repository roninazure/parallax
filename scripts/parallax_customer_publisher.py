#!/usr/bin/env python3
"""Single writer for the three certified PARALLAX customer feeds.

The parallax.public.v1 files alone decide which contracts are BUYs. The alert
database and the NFL slate/title are used only for customer display metadata.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

LANES = ("nfl", "cfb", "mlb")
SOURCE_SCHEMA = "parallax.public.v1"
CUSTOMER_SCHEMA = "parallax.customer.v1"
MAX_AGE = timedelta(minutes=90)
HEARTBEAT = timedelta(minutes=15)
NFL_TEAMS = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LA": "Los Angeles Rams", "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}
TITLE = re.compile(r"^Who will win in the upcoming football event (.+?) vs (.+?) scheduled for .+\?$", re.I)


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return dt.astimezone(timezone.utc)


def venue(value: object) -> str:
    result = str(value or "").upper()
    return "POLYMARKET" if result == "PMUS" else result


def read_sources(directory: Path, now: datetime) -> tuple[dict, dict, dict]:
    sources, hashes, health = {}, {}, {}
    for lane in LANES:
        path = directory / f"{lane}.json"
        count, age, generated_at = None, None, None
        try:
            raw = path.read_bytes()
            hashes[lane] = hashlib.sha256(raw).hexdigest()
            feed = json.loads(raw)
            if not isinstance(feed, dict) or feed.get("schema_version") != SOURCE_SCHEMA or feed.get("lane") != lane.upper():
                raise ValueError("wrong source schema or lane")
            if feed.get("read_only") is not True or not isinstance(feed.get("plays"), list) or not isinstance(feed.get("summary"), dict):
                raise ValueError("malformed sanitized feed")
            count = feed["summary"].get("buy")
            if type(count) is not int or count < 0:
                raise ValueError("invalid source BUY count")
            generated_at = feed.get("generated_at")
            generated = timestamp(generated_at)
            age = now - generated
            if not timedelta(0) <= age <= MAX_AGE:
                raise ValueError("source feed stale or future dated")
            if count and feed.get("buy_publication_eligible") is not True:
                raise ValueError("BUY publication disabled")
            sources[lane] = feed
            health[lane] = {"source_state": "FRESH", "source_buy": count, "source_age_seconds": int(age.total_seconds())}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            if lane not in hashes:
                hashes[lane] = None
            # Suppress the affected lane; never reuse its previous LIVE payload.
            sources[lane] = None
            source_state = "STALE" if "stale or future dated" in str(exc) else "UNAVAILABLE"
            health[lane] = {"source_state": source_state, "source_buy": count,
                            "source_age_seconds": int(age.total_seconds()) if age is not None else None,
                            "error": str(exc)[:240]}
            health[lane]["source_feed_generated_at"] = generated_at
    return sources, hashes, health


def assert_sources_unchanged(directory: Path, hashes: dict) -> None:
    for lane in LANES:
        path = directory / f"{lane}.json"
        current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if current != hashes[lane]:
            raise RuntimeError(f"{lane}: source changed during publication")


def nfl_games(source: dict) -> list[dict]:
    slate = source.get("slate")
    if not isinstance(slate, dict) or not isinstance(slate.get("dates"), list):
        raise ValueError("nfl: missing sanitized slate")
    games = []
    for day in slate["dates"]:
        if not isinstance(day, dict) or not isinstance(day.get("games"), list):
            raise ValueError("nfl: malformed sanitized slate")
        games.extend(day["games"])
    return games


def nfl_display(play: dict, games: list[dict]) -> tuple[str, str] | None:
    """Match a feed BUY to exactly one sanitized slate game, without creating BUYs."""
    market_id = str(play.get("market_id") or "")
    candidates = []
    if venue(play.get("venue")) == "POLYMARKET":
        match = TITLE.fullmatch(str(play.get("market_title") or ""))
        if match:
            candidates = [g for g in games if NFL_TEAMS.get(g.get("away_team")) == match[1]
                          and NFL_TEAMS.get(g.get("home_team")) == match[2]]
    elif venue(play.get("venue")) == "KALSHI":
        candidates = [g for g in games if isinstance(g, dict) and
                      market_id.startswith("KXNFLGAME-" +
                          datetime.fromisoformat(str(g.get("date"))).strftime("%y%b%d").upper() +
                          str(g.get("away_team")) + str(g.get("home_team")) + "-")]
    if len(candidates) != 1:
        return None
    game = candidates[0]
    if game.get("schedule_status") != "SCHEDULED" or game.get("status") != "BUY":
        return None
    start = game.get("start_time")
    timestamp(start)
    return f"{game['away_team']} at {game['home_team']}", start


def enrichment(db: Path, sport: str, normalized_venue: str, market_id: str, side: str) -> dict:
    """Read display fields using the full contract identity and a read-only URI."""
    if not db.is_file():
        return {}
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(
            "SELECT matchup, game_start FROM buy_alert_state "
            "WHERE sport=? AND venue=? AND market_id=? AND side=?",
            (sport, normalized_venue, market_id, side),
        ).fetchone()
    return dict(row) if row else {}


def build(lane: str, source: dict, db: Path, now: datetime) -> dict:
    buys, seen = [], set()
    games = nfl_games(source) if lane == "nfl" else []
    for play in source["plays"]:
        if not isinstance(play, dict):
            raise ValueError(f"{lane}: malformed play")
        if play.get("action") != "BUY":
            continue
        if play.get("publication_eligible") is not True:
            raise ValueError(f"{lane}: uncertified source BUY")
        sport = str(play.get("sport") or "").upper()
        normalized_venue = venue(play.get("venue"))
        market_id = str(play.get("market_id") or "")
        side = str(play.get("contract_side") or "").upper()
        key = sport, normalized_venue, market_id, side
        if sport != lane.upper() or normalized_venue not in {"POLYMARKET", "KALSHI"} or not market_id or side not in {"YES", "NO"} or key in seen:
            raise ValueError(f"{lane}: invalid or duplicate BUY identity {key}")
        seen.add(key)
        row = enrichment(db, *key)
        display = nfl_display(play, games) if lane == "nfl" else None
        if display and row.get("matchup") and row["matchup"] != display[0]:
            raise ValueError(f"{lane}: certified BUY {market_id} has conflicting display metadata")
        matchup = play.get("matchup") or row.get("matchup") or (display[0] if display else None)
        start = (display[1] if display else None) or play.get("resolution_time") or row.get("game_start")
        if not isinstance(matchup, str) or not matchup.strip() or not start:
            raise ValueError(f"{lane}: certified BUY {market_id} missing matchup/start_time")
        timestamp(start)
        numeric = {field: play.get(field) for field in ("price", "model_probability", "edge_pp")}
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in numeric.values()):
            raise ValueError(f"{lane}: certified BUY {market_id} has invalid numeric field")
        if not 0 < numeric["price"] < 1 or not 0 < numeric["model_probability"] < 1:
            raise ValueError(f"{lane}: certified BUY {market_id} has invalid price/probability")
        buys.append({"sport": sport, "venue": normalized_venue, "market_id": market_id,
                     "contract_side": side, "matchup": matchup.strip(), "start_time": start, **numeric})
    expected = source["summary"]["buy"]
    if len(buys) != expected:
        raise ValueError(f"{lane}: BUY mismatch source={expected} customer={len(buys)}")
    slate = source.get("slate")
    schedule = slate.get("schedule_state") if isinstance(slate, dict) else None
    if not isinstance(schedule, str) or not schedule:
        schedule = "COMPLETE" if source.get("market_data_complete") is True else "INCOMPLETE" if source.get("market_data_complete") is False else "UNVERIFIED"
    return {"schema_version": CUSTOMER_SCHEMA, "lane": lane.upper(), "generated_at": now.isoformat(),
            "source_feed_generated_at": source["generated_at"], "source_schedule_state": schedule,
            "read_only": True, "state": "LIVE", "summary": {"buy": len(buys)}, "plays": buys}


def build_all(directory: Path, db: Path, now: datetime) -> tuple[dict, dict, dict]:
    sources, hashes, health = read_sources(directory, now)
    expected = {}
    for lane in LANES:
        source = sources[lane]
        if source is None:
            expected[lane] = {"schema_version": CUSTOMER_SCHEMA, "lane": lane.upper(),
                "generated_at": now.isoformat(), "source_feed_generated_at": health[lane].get("source_feed_generated_at"),
                "source_schedule_state": "STALE", "read_only": True, "state": "STALE",
                "summary": {"buy": 0}, "plays": []}
        else:
            expected[lane] = build(lane, source, db, now)
    return expected, hashes, health


def build_stable_snapshot(directory: Path, db: Path, now: datetime) -> tuple[dict, dict, dict]:
    """Build from one immutable read, retrying if an atomic feed replacement races it."""
    snapshot_time = now
    for attempt in range(3):
        snapshot = build_all(directory, db, snapshot_time)
        try:
            assert_sources_unchanged(directory, snapshot[1])
            return snapshot
        except RuntimeError as exc:
            if "source changed during publication" not in str(exc) or attempt == 2:
                raise
            snapshot_time = max(datetime.now(timezone.utc), snapshot_time)
    raise RuntimeError("source snapshot could not be stabilized")


def git(worktree: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(["/usr/bin/git", "-C", str(worktree), *args], text=True,
                        capture_output=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if check and cp.returncode:
        raise RuntimeError(f"git {args[0]} failed: {(cp.stderr or cp.stdout).strip()[:400]}")
    return cp


def checked_worktree(worktree: Path) -> str:
    if Path(git(worktree, "rev-parse", "--show-toplevel").stdout.strip()).resolve() != worktree.resolve():
        raise RuntimeError("wrong customer worktree path")
    if git(worktree, "branch", "--show-current").stdout.strip() != "parallax-live-data":
        raise RuntimeError("wrong customer worktree branch")
    if git(worktree, "status", "--porcelain", "--untracked-files=all").stdout.strip():
        raise RuntimeError("dirty customer worktree")
    if git(worktree, "remote", "get-url", "--push", "origin").stdout.strip() != "git@github.com:roninazure/parallax.git":
        raise RuntimeError("wrong customer worktree remote")
    return git(worktree, "rev-parse", "HEAD").stdout.strip()


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(payload, handle, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def material(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if key not in {"generated_at", "source_feed_generated_at"}}


def verify_payloads(worktree: Path, expected: dict, revision: str) -> dict:
    result = {}
    for lane in LANES:
        raw = git(worktree, "show", f"{revision}:feeds/{lane}.json").stdout
        actual = json.loads(raw)
        if actual.get("schema_version") != CUSTOMER_SCHEMA or actual.get("state") != expected[lane].get("state") or actual.get("summary", {}).get("buy") != expected[lane]["summary"]["buy"] or actual.get("plays") != expected[lane]["plays"]:
            raise RuntimeError(f"{lane}: remote customer BUY mismatch")
        stale = expected[lane].get("state") == "STALE"
        result[lane] = {"source_buy": None if stale else expected[lane]["summary"]["buy"],
                        "customer_buy": actual["summary"]["buy"], "source_state": "UNAVAILABLE" if stale else "FRESH",
                        "parity": "STALE_SUPPRESSED" if stale else "EQUAL"}
    return result


def recover_stranded_commit(worktree: Path, remote: str, head: str, expected: dict) -> bool:
    """Recover a stranded publisher commit, or discard it if a newer snapshot superseded it.

    Returns True when the commit was pushed and False when the verified publisher-only
    commit was safely discarded so the caller can publish the current snapshot.
    """
    ancestor = git(worktree, "merge-base", "--is-ancestor", remote, head, check=False)
    if ancestor.returncode != 0:
        raise RuntimeError("customer worktree is behind or diverged from origin/parallax-live-data")
    commits = git(worktree, "rev-list", f"{remote}..{head}").stdout.splitlines()
    if not commits:
        raise RuntimeError("stranded customer worktree has no recoverable commits")
    allowed = {f"feeds/{lane}.json" for lane in LANES}
    for commit in commits:
        subject = git(worktree, "show", "-s", "--format=%s", commit).stdout.strip()
        paths = git(worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).stdout.splitlines()
        if not subject.startswith("data(parallax): unified customer publish ") or not paths or not set(paths) <= allowed:
            raise RuntimeError("stranded commit contains unexpected customer-worktree changes")
    changed = git(worktree, "diff", "--name-only", f"{remote}..{head}").stdout.splitlines()
    if not changed or not set(changed) <= allowed:
        raise RuntimeError("stranded commit contains unexpected customer-worktree changes")
    matches_snapshot = True
    for lane in LANES:
        actual = json.loads(git(worktree, "show", f"{head}:feeds/{lane}.json").stdout)
        if material(actual) != material(expected[lane]):
            matches_snapshot = False
    if matches_snapshot:
        verify_payloads(worktree, expected, head)
    live = git(worktree, "ls-remote", "--heads", "origin", "parallax-live-data").stdout.split()
    if len(live) != 2 or live[0] != remote:
        raise RuntimeError("remote branch changed before stranded commit recovery")
    if not matches_snapshot:
        # checked_worktree established a clean tree, and every unpushed commit above
        # was created by this publisher and touched only customer feed files.
        git(worktree, "reset", "--hard", remote)
        return False
    push = git(worktree, "push", "origin", "HEAD:refs/heads/parallax-live-data", check=False)
    if push.returncode:
        raise RuntimeError(f"stranded commit recovery push failed: {(push.stderr or push.stdout).strip()[:400]}")
    return True


def send_failure_notification(message: str, title: str = "PARALLAX customer publisher failure") -> bool:
    """Send an actionable failure through the existing ntfy configuration."""
    config_path = Path.home() / "Library/Application Support/SwarmEdge/parallax-alerts.env"
    values = {}
    try:
        if config_path.exists() and config_path.stat().st_mode & 0o077 == 0:
            for raw in config_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    for key in ("PARALLAX_ALERT_MODE", "PARALLAX_ALERT_NTFY_TOPIC", "PARALLAX_ALERT_NTFY_SERVER"):
        if key in os.environ:
            values[key] = os.environ[key]
    if values.get("PARALLAX_ALERT_MODE", "disabled").strip().casefold() != "ntfy":
        return False
    topic = values.get("PARALLAX_ALERT_NTFY_TOPIC", "").strip()
    if not topic:
        return False
    server = values.get("PARALLAX_ALERT_NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")
    request = Request(
        f"{server}/",
        data=json.dumps({"topic": topic, "title": title,
                         "message": message, "priority": 4}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        response.read(1024)
    return True


def degradation_health(state: Path, health: dict, previous: dict, now: datetime) -> dict:
    """Notify only for sustained degraded operation and its recovery."""
    if health["state"] == "DEGRADED":
        was_degraded = previous.get("state") == "DEGRADED"
        first = previous.get("failure_started_at") if was_degraded else now.isoformat()
        last_alert = previous.get("last_failure_alert_at") if was_degraded else None
        result = {"failure_started_at": first,
                  "consecutive_failures": int(previous.get("consecutive_failures", 0)) + 1 if was_degraded else 1,
                  "last_failure_alert_at": last_alert}
        if now - timestamp(first) >= timedelta(minutes=5) and (
                not last_alert or now - timestamp(last_alert) >= timedelta(hours=6)):
            lanes = [lane.upper() for lane, detail in health["lanes"].items() if detail["source_state"] != "FRESH"]
            try:
                if send_failure_notification("Stale or unavailable source feed(s): " + ", ".join(lanes)):
                    result["last_failure_alert_at"] = now.isoformat()
            except Exception as exc:
                result["last_failure_alert_at"] = now.isoformat()
                result["notification_error"] = type(exc).__name__
        return result
    if previous.get("state") in {"DEGRADED", "FAILED"} and previous.get("last_failure_alert_at"):
        try:
            if send_failure_notification("All source lanes are fresh and publishing normally.",
                                         "PARALLAX customer publisher recovered"):
                return {"last_recovery_notification_at": now.isoformat()}
        except Exception as exc:
            return {"notification_error": type(exc).__name__}
    return {}


def failure_health(state: Path, exc: Exception, now: datetime) -> dict:
    previous = {}
    try:
        previous = json.loads((state / "customer_publisher_health.json").read_text())
    except (OSError, json.JSONDecodeError):
        pass
    was_failed = previous.get("state") == "FAILED"
    first_failure = previous.get("failure_started_at") if was_failed else now.isoformat()
    count = int(previous.get("consecutive_failures", 0)) + 1 if was_failed else 1
    last_alert = previous.get("last_failure_alert_at") if was_failed else None
    health = {"state": "FAILED", "checked_at": now.isoformat(),
              "error": f"{type(exc).__name__}: {exc}",
              "failure_started_at": first_failure, "consecutive_failures": count,
              "last_failure_alert_at": last_alert,
              "publisher_release_sha": os.environ.get("PARALLAX_PUBLISHER_SHA")}
    try:
        started = timestamp(first_failure)
        eligible = now - started >= timedelta(minutes=5)
        cooldown_done = not last_alert or now - timestamp(last_alert) >= timedelta(hours=6)
        if eligible and cooldown_done:
            detail = str(exc).replace("\n", " ")[:500]
            try:
                if send_failure_notification(f"Customer publisher has failed for at least 5 minutes. {detail}"):
                    health["last_failure_alert_at"] = now.isoformat()
            except Exception as alert_exc:
                # Cool down failed ntfy attempts too, so an outage cannot create a retry storm.
                health["last_failure_alert_at"] = now.isoformat()
                health["notification_error"] = type(alert_exc).__name__
    except Exception:
        # Notification failure must never mask the publishing failure or repeat every minute.
        pass
    return health


def publish(worktree: Path, directory: Path, db: Path, state: Path, now: datetime) -> dict:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "customer_publisher.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("publisher already running") from exc
        try:
            head = checked_worktree(worktree)
            git(worktree, "fetch", "origin", "parallax-live-data")
            remote_before = git(worktree, "rev-parse", "origin/parallax-live-data").stdout.strip()
            expected, hashes, lane_health = build_stable_snapshot(directory, db, now)
            recovered_pending_commit = False
            if head != remote_before:
                recovered_pending_commit = recover_stranded_commit(worktree, remote_before, head, expected)
                if not recovered_pending_commit:
                    head = remote_before
            changed = []
            for lane in LANES:
                path = worktree / "feeds" / f"{lane}.json"
                old = json.loads(path.read_text())
                old_source_at = old.get("source_feed_generated_at") if isinstance(old, dict) else None
                old_at = timestamp(old_source_at) if old_source_at else now
                expected_source_at = expected[lane].get("source_feed_generated_at")
                heartbeat_due = bool(expected_source_at) and timestamp(expected_source_at) - old_at >= HEARTBEAT
                if material(old) != material(expected[lane]) or heartbeat_due:
                    changed.append(lane)
                else:
                    expected[lane] = old
            for lane in changed:
                atomic_json(worktree / "feeds" / f"{lane}.json", expected[lane])
            if changed:
                git(worktree, "add", "--", *(f"feeds/{lane}.json" for lane in LANES))
                git(worktree, "commit", "-m", f"data(parallax): unified customer publish {now.isoformat()}")
                new_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
                push = git(worktree, "push", "origin", "HEAD:refs/heads/parallax-live-data", check=False)
                if push.returncode:
                    raise RuntimeError(f"push race or failure: {(push.stderr or push.stdout).strip()[:400]}")
            else:
                new_head = head
            remote_sha = git(worktree, "ls-remote", "--heads", "origin", "parallax-live-data").stdout.split()[0]
            if remote_sha != new_head:
                raise RuntimeError(f"remote SHA mismatch expected={new_head} actual={remote_sha}")
            counts = verify_payloads(worktree, expected, remote_sha)
            for lane in LANES:
                counts[lane].update(lane_health[lane])
                if lane_health[lane]["source_state"] != "FRESH":
                    counts[lane]["parity"] = "STALE_SUPPRESSED"
            degraded = any(lane_health[lane]["source_state"] != "FRESH" for lane in LANES)
            previous_health = {}
            try:
                previous_health = json.loads((state / "customer_publisher_health.json").read_text())
            except (OSError, json.JSONDecodeError):
                pass
            health = {"state": "DEGRADED" if degraded else "HEALTHY", "checked_at": datetime.now(timezone.utc).isoformat(),
                      "remote_head": remote_sha, "changed_lanes": changed, "lanes": counts,
                      "recovered_pending_commit": recovered_pending_commit,
                      "publisher_release_sha": os.environ.get("PARALLAX_PUBLISHER_SHA")}
            health.update(degradation_health(state, health, previous_health, datetime.now(timezone.utc)))
            atomic_json(state / "customer_publisher_health.json", health)
            return health
        except Exception as exc:
            atomic_json(state / "customer_publisher_health.json",
                        failure_health(state, exc, datetime.now(timezone.utc)))
            raise


def main(argv: list[str] | None = None) -> int:
    home = Path.home()
    base = home / "Library/Application Support/SwarmEdge/state"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="read feeds/DB/worktree only; no lock, fetch, health write or push")
    parser.add_argument("--public-feed", type=Path, default=base / "parallax/public_feed")
    parser.add_argument("--db", type=Path, default=base / "parallax_inbox.sqlite")
    parser.add_argument("--state", type=Path, default=base / "parallax")
    parser.add_argument("--worktree", type=Path, default=home / "swarm-runtime/swarm-edge-live-data")
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            checked_worktree(args.worktree)
            expected, _hashes, lane_health = build_stable_snapshot(
                args.public_feed, args.db, datetime.now(timezone.utc))
            print("DRY_RUN_OK " + " ".join(f"{lane.upper()}={expected[lane]['summary']['buy']}:{lane_health[lane]['source_state']}" for lane in LANES))
        else:
            health = publish(args.worktree, args.public_feed, args.db, args.state, datetime.now(timezone.utc))
            print(health["state"] + " " + " ".join(f"{lane.upper()}={health['lanes'][lane]['customer_buy']}" for lane in LANES) + f" head={health['remote_head']}")
        return 0
    except Exception as exc:
        print(f"FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
