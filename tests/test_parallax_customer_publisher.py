"""Contract and failure tests for the single customer publisher."""

from __future__ import annotations

import fcntl
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import deploy_parallax_customer_publisher as deployment
from scripts import parallax_customer_publisher as publisher

NOW = datetime(2026, 10, 8, 0, 30, tzinfo=timezone.utc)


def play(market_id="1083096", *, title=True, matchup=None, start="2026-10-11T17:00:00+00:00"):
    result = {"action": "BUY", "publication_eligible": True, "sport": "NFL", "venue": "PMUS",
              "market_id": market_id, "contract_side": "NO", "matchup": matchup,
              "resolution_time": start, "price": 0.4, "model_probability": 0.6, "edge_pp": 20.0}
    if title:
        result["market_title"] = "Who will win in the upcoming football event Indianapolis Colts vs Pittsburgh Steelers scheduled for October 11, 2026 at 5:00 PM UTC?"
    return result


def source(lane="nfl", plays=None, buy=None, generated_at=None):
    plays = [play()] if plays is None and lane == "nfl" else (plays or [])
    result = {"schema_version": "parallax.public.v1", "lane": lane.upper(), "read_only": True,
              "generated_at": generated_at or NOW.isoformat(), "buy_publication_eligible": True,
              "summary": {"buy": len(plays) if buy is None else buy}, "plays": plays}
    if lane == "nfl":
        result["slate"] = {"dates": [{"games": [{"away_team": "IND", "home_team": "PIT",
            "date": "2026-10-11", "schedule_status": "SCHEDULED", "status": "BUY",
            "start_time": "2026-10-11T17:00:00+00:00"}]}]}
    return result


def fixtures(tmp_path):
    feed = tmp_path / "public_feed"
    feed.mkdir()
    for lane in publisher.LANES:
        (feed / f"{lane}.json").write_text(json.dumps(source(lane)))
    db = tmp_path / "alerts.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE buy_alert_state (sport TEXT, venue TEXT, market_id TEXT, side TEXT, matchup TEXT, game_start TEXT)")
    return feed, db


def test_missing_nfl_matchup_1083096_uses_only_sanitized_title_and_slate(tmp_path):
    feed, db = fixtures(tmp_path)
    expected, _, _ = publisher.build_all(feed, db, NOW)
    assert expected["nfl"]["summary"]["buy"] == 1
    assert expected["nfl"]["plays"][0]["matchup"] == "IND at PIT"
    assert expected["nfl"]["plays"][0]["start_time"] == "2026-10-11T17:00:00+00:00"


def test_db_enrichment_requires_exact_sport_venue_market_and_side(tmp_path):
    feed, db = fixtures(tmp_path)
    cfb = source("cfb", [{**play("777", title=False, start=None), "sport": "CFB"}])
    (feed / "cfb.json").write_text(json.dumps(cfb))
    with sqlite3.connect(db) as con:
        con.executemany("INSERT INTO buy_alert_state VALUES (?,?,?,?,?,?)", [
            ("CFB", "POLYMARKET", "777", "YES", "wrong side", "2026-10-10T00:00:00+00:00"),
            ("CFB", "KALSHI", "777", "NO", "wrong venue", "2026-10-10T00:00:00+00:00"),
            ("NFL", "POLYMARKET", "777", "NO", "wrong sport", "2026-10-10T00:00:00+00:00"),
        ])
    with pytest.raises(ValueError, match="missing matchup/start_time"):
        publisher.build_all(feed, db, NOW)
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO buy_alert_state VALUES (?,?,?,?,?,?)",
                    ("CFB", "POLYMARKET", "777", "NO", "Texas at Oklahoma", "2026-10-10T19:30:00+00:00"))
    expected, _, _ = publisher.build_all(feed, db, NOW)
    assert expected["cfb"]["plays"][0]["matchup"] == "Texas at Oklahoma"
    assert expected["cfb"]["plays"][0]["start_time"] == "2026-10-10T19:30:00+00:00"


def test_all_lane_source_customer_count_equality_is_mandatory(tmp_path):
    feed, db = fixtures(tmp_path)
    (feed / "nfl.json").write_text(json.dumps(source(buy=2)))
    with pytest.raises(ValueError, match="BUY mismatch source=2 customer=1"):
        publisher.build_all(feed, db, NOW)
    (feed / "nfl.json").write_text(json.dumps(source(plays=[{**play(), "publication_eligible": False}])))
    with pytest.raises(ValueError, match="uncertified source BUY"):
        publisher.build_all(feed, db, NOW)


def test_valid_zero_buy_nfl_snapshot_publishes_live_empty_feed(tmp_path):
    feed, db = fixtures(tmp_path)
    (feed / "nfl.json").write_text(json.dumps(source("nfl", plays=[], buy=0)))
    expected, _, health = publisher.build_all(feed, db, NOW)
    assert health["nfl"]["source_state"] == "FRESH"
    assert expected["nfl"]["state"] == "LIVE"
    assert expected["nfl"]["summary"]["buy"] == 0
    assert expected["nfl"]["plays"] == []


def test_source_read_is_an_immutable_snapshot_after_atomic_replacement(tmp_path):
    feed, _db = fixtures(tmp_path)
    snapshot, hashes, _ = publisher.read_sources(feed, NOW)
    old_market = snapshot["nfl"]["plays"][0]["market_id"]
    (feed / "nfl.json").write_text(json.dumps(source("nfl", plays=[], buy=0)))
    assert snapshot["nfl"]["plays"][0]["market_id"] == old_market
    with pytest.raises(RuntimeError, match="nfl: source changed during publication"):
        publisher.assert_sources_unchanged(feed, hashes)


def test_snapshot_capture_retries_an_atomic_nfl_update(monkeypatch, tmp_path):
    feed, db = fixtures(tmp_path)
    original_assert = publisher.assert_sources_unchanged
    attempts = 0

    def replace_during_first_validation(directory, hashes):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            (feed / "nfl.json").write_text(json.dumps(source(
                "nfl", plays=[], buy=0, generated_at=datetime.now(timezone.utc).isoformat())))
        return original_assert(directory, hashes)

    monkeypatch.setattr(publisher, "assert_sources_unchanged", replace_during_first_validation)
    expected, _hashes, health = publisher.build_stable_snapshot(feed, db, datetime.now(timezone.utc))
    assert attempts == 2
    assert health["nfl"]["source_state"] == "FRESH"
    assert expected["nfl"]["summary"]["buy"] == 0
    assert expected["nfl"]["plays"] == []


def test_stale_cfb_is_suppressed_while_fresh_nfl_and_mlb_remain_publishable(tmp_path):
    feed, db = fixtures(tmp_path)
    stale_cfb = source("cfb", plays=[{"action": "BUY", "publication_eligible": True}], generated_at="2026-10-07T22:00:00+00:00")
    (feed / "cfb.json").write_text(json.dumps(stale_cfb))
    expected, hashes, lanes = publisher.build_all(feed, db, NOW)
    publisher.assert_sources_unchanged(feed, hashes)
    assert lanes["cfb"]["source_state"] == "STALE"
    assert lanes["cfb"]["source_buy"] == 1
    assert expected["cfb"]["state"] == "STALE"
    assert expected["cfb"]["summary"]["buy"] == 0 and expected["cfb"]["plays"] == []
    for lane in ("nfl", "mlb"):
        assert lanes[lane]["source_state"] == "FRESH"
        assert expected[lane]["state"] == "LIVE"
        assert expected[lane]["summary"]["buy"] == lanes[lane]["source_buy"]


def test_unavailable_cfb_scan_does_not_block_other_lanes(tmp_path):
    feed, db = fixtures(tmp_path)
    (feed / "cfb.json").unlink()
    expected, hashes, lanes = publisher.build_all(feed, db, NOW)
    assert expected["cfb"]["state"] == "STALE"
    assert lanes["cfb"]["source_state"] == "UNAVAILABLE"
    publisher.assert_sources_unchanged(feed, hashes)
    (feed / "cfb.json").write_text(json.dumps(source("cfb")))
    with pytest.raises(RuntimeError, match="changed during publication"):
        publisher.assert_sources_unchanged(feed, hashes)


def test_recovery_notification_only_follows_previously_alerted_degradation(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(publisher, "send_failure_notification",
                        lambda message, title="failure": sent.append((title, message)) or True)
    prior = {"state": "DEGRADED", "last_failure_alert_at": NOW.isoformat()}
    result = publisher.degradation_health(tmp_path, {"state": "HEALTHY"}, prior, NOW)
    assert result["last_recovery_notification_at"] == NOW.isoformat()
    assert sent[0][0] == "PARALLAX customer publisher recovered"
    assert publisher.degradation_health(tmp_path, {"state": "HEALTHY"}, {"state": "DEGRADED"}, NOW) == {}
    assert len(sent) == 1


def test_sustained_cfb_degradation_alerts_once_after_threshold(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(publisher, "send_failure_notification", lambda message: sent.append(message) or True)
    health = {"state": "DEGRADED", "lanes": {"cfb": {"source_state": "UNAVAILABLE"}}}
    first = publisher.degradation_health(tmp_path, health, {}, NOW)
    assert sent == []
    first.update({"state": "DEGRADED"})
    later = publisher.degradation_health(tmp_path, health, first, NOW.replace(minute=36))
    assert len(sent) == 1 and "CFB" in sent[0]
    assert later["last_failure_alert_at"] == NOW.replace(minute=36).isoformat()


def test_wrong_and_dirty_customer_worktree_fail_closed(monkeypatch, tmp_path):
    def fake_git(_worktree, *args, **_kwargs):
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return subprocess.CompletedProcess(args, 0, str(tmp_path) + "\n", "")
        if args[0] == "branch":
            return subprocess.CompletedProcess(args, 0, "main\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(publisher, "git", fake_git)
    with pytest.raises(RuntimeError, match="wrong customer worktree"):
        publisher.checked_worktree(tmp_path)
    def dirty_git(_worktree, *args, **_kwargs):
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return subprocess.CompletedProcess(args, 0, str(tmp_path) + "\n", "")
        return subprocess.CompletedProcess(args, 0, "parallax-live-data\n" if args[0] == "branch" else " M feeds/nfl.json\n", "")
    monkeypatch.setattr(publisher, "git", dirty_git)
    with pytest.raises(RuntimeError, match="dirty customer worktree"):
        publisher.checked_worktree(tmp_path)


def test_lock_contention_does_not_touch_health_or_worktree(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    lock = state / "customer_publisher.lock"
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            publisher.publish(tmp_path / "worktree", tmp_path / "feeds", tmp_path / "db", state, NOW)
    assert not (state / "customer_publisher_health.json").exists()


def test_push_race_marks_health_failed_and_never_claims_remote_sha(monkeypatch, tmp_path):
    feed, db = fixtures(tmp_path)
    worktree, state = tmp_path / "worktree", tmp_path / "state"
    (worktree / "feeds").mkdir(parents=True)
    for lane in publisher.LANES:
        (worktree / "feeds" / f"{lane}.json").write_text(json.dumps({"schema_version": "parallax.customer.v1", "summary": {"buy": 0}, "plays": [], "source_feed_generated_at": NOW.isoformat()}))
    monkeypatch.setattr(publisher, "checked_worktree", lambda _path: "a" * 40)
    calls = []
    def fake_git(_worktree, *args, **kwargs):
        calls.append(args)
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, ("a" if args[1] == "origin/parallax-live-data" else "b") * 40 + "\n", "")
        if args[0] == "push":
            return subprocess.CompletedProcess(args, 1, "", "non-fast-forward")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(publisher, "git", fake_git)
    with pytest.raises(RuntimeError, match="push race or failure"):
        publisher.publish(worktree, feed, db, state, NOW)
    health = json.loads((state / "customer_publisher_health.json").read_text())
    assert health["state"] == "FAILED"
    assert "remote_head" not in health
    assert [args[0] for args in calls].count("push") == 1


def test_remote_payload_verification_rejects_count_drift(monkeypatch, tmp_path):
    def fake_git(_worktree, *args, **_kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps({"schema_version": publisher.CUSTOMER_SCHEMA,
            "summary": {"buy": 0}, "plays": []}), "")
    monkeypatch.setattr(publisher, "git", fake_git)
    expected = {lane: {"summary": {"buy": 1}, "plays": [{}]} for lane in publisher.LANES}
    with pytest.raises(RuntimeError, match="remote customer BUY mismatch"):
        publisher.verify_payloads(tmp_path, expected, "f" * 40)


def test_stranded_publisher_commit_recovers_only_by_verified_fast_forward(monkeypatch, tmp_path):
    feed, db = fixtures(tmp_path)
    expected, _, _ = publisher.build_all(feed, db, NOW)
    calls = []
    def fake_git(_worktree, *args, **kwargs):
        calls.append(args)
        if args[:2] == ("merge-base", "--is-ancestor"):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "rev-list":
            return subprocess.CompletedProcess(args, 0, "b" * 40 + "\n", "")
        if args[:3] == ("show", "-s", "--format=%s"):
            return subprocess.CompletedProcess(args, 0, "data(parallax): unified customer publish retry\n", "")
        if args[0] == "diff-tree":
            return subprocess.CompletedProcess(args, 0, "feeds/nfl.json\n", "")
        if args[0] == "ls-remote":
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\trefs/heads/parallax-live-data\n", "")
        if args[:2] == ("diff", "--name-only"):
            return subprocess.CompletedProcess(args, 0, "feeds/nfl.json\nfeeds/cfb.json\nfeeds/mlb.json\n", "")
        if args[0] == "show":
            lane = args[1].split("feeds/")[-1].removesuffix(".json")
            return subprocess.CompletedProcess(args, 0, json.dumps(expected[lane]), "")
        if args[0] == "push":
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)
    monkeypatch.setattr(publisher, "git", fake_git)
    publisher.recover_stranded_commit(tmp_path, "a" * 40, "b" * 40, expected)
    push = next(call for call in calls if call[0] == "push")
    assert push == ("push", "origin", "HEAD:refs/heads/parallax-live-data")
    assert all("--force" not in call for call in calls)


def test_stale_stranded_publisher_commit_is_discarded_before_current_snapshot_publish(monkeypatch, tmp_path):
    remote, head = "a" * 40, "b" * 40
    old_feed = {lane: {"schema_version": publisher.CUSTOMER_SCHEMA, "state": "LIVE",
                       "summary": {"buy": 1}, "plays": [{"market_id": "old"}]}
                for lane in publisher.LANES}
    expected = {lane: {"schema_version": publisher.CUSTOMER_SCHEMA, "state": "LIVE",
                       "summary": {"buy": 0}, "plays": []}
                for lane in publisher.LANES}
    calls = []

    def fake_git(_worktree, *args, **kwargs):
        calls.append(args)
        if args[:2] == ("merge-base", "--is-ancestor"):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "rev-list":
            return subprocess.CompletedProcess(args, 0, head + "\n", "")
        if args[:3] == ("show", "-s", "--format=%s"):
            return subprocess.CompletedProcess(args, 0, "data(parallax): unified customer publish retry\n", "")
        if args[0] == "diff-tree":
            return subprocess.CompletedProcess(args, 0, "feeds/nfl.json\n", "")
        if args[:2] == ("diff", "--name-only"):
            return subprocess.CompletedProcess(args, 0, "feeds/nfl.json\n", "")
        if args[0] == "show":
            lane = args[1].split("feeds/")[-1].removesuffix(".json")
            return subprocess.CompletedProcess(args, 0, json.dumps(old_feed[lane]), "")
        if args[0] == "ls-remote":
            return subprocess.CompletedProcess(args, 0, remote + "\trefs/heads/parallax-live-data\n", "")
        if args[0] == "reset":
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(publisher, "git", fake_git)
    assert publisher.recover_stranded_commit(tmp_path, remote, head, expected) is False
    assert ("reset", "--hard", remote) in calls
    assert not any(call[0] == "push" for call in calls)


def test_ntfy_failure_alert_uses_configured_topic_and_sends_existing_alert(monkeypatch, tmp_path):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return b"ok"

    config_dir = tmp_path / "Library/Application Support/SwarmEdge"
    config_dir.mkdir(parents=True)
    config = config_dir / "parallax-alerts.env"
    config.write_text("PARALLAX_ALERT_MODE=ntfy\nPARALLAX_ALERT_NTFY_TOPIC=parallax-test\nPARALLAX_ALERT_NTFY_SERVER=https://ntfy.example\n")
    config.chmod(0o600)
    monkeypatch.setattr(publisher.Path, "home", lambda: tmp_path)

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(publisher, "urlopen", fake_urlopen)
    assert publisher.send_failure_notification("publisher failed") is True
    request, timeout = requests[0]
    assert request.full_url == "https://ntfy.example/"
    assert json.loads(request.data) == {
        "topic": "parallax-test", "title": "PARALLAX customer publisher failure",
        "message": "publisher failed", "priority": 4,
    }
    assert timeout == 5


def test_stranded_publisher_commit_recovers_against_local_bare_remote(tmp_path):
    feed, db = fixtures(tmp_path)
    expected, _, _ = publisher.build_all(feed, db, NOW)
    remote, seed, worktree = tmp_path / "remote.git", tmp_path / "seed", tmp_path / "worktree"
    def run_git(*args):
        return subprocess.run(["/usr/bin/git", *map(str, args)], text=True, capture_output=True, check=True)
    run_git("init", "--bare", "--initial-branch=parallax-live-data", remote)
    run_git("init", "--initial-branch=parallax-live-data", seed)
    run_git("-C", seed, "config", "user.name", "Publisher Test")
    run_git("-C", seed, "config", "user.email", "publisher@example.test")
    (seed / "feeds").mkdir()
    for lane in publisher.LANES:
        (seed / "feeds" / f"{lane}.json").write_text(json.dumps({
            "schema_version": publisher.CUSTOMER_SCHEMA, "lane": lane.upper(),
            "summary": {"buy": 0}, "plays": [],
            "source_feed_generated_at": "2026-10-07T00:00:00+00:00"}))
    run_git("-C", seed, "add", "feeds")
    run_git("-C", seed, "commit", "-m", "initial customer feeds")
    run_git("-C", seed, "remote", "add", "origin", remote)
    run_git("-C", seed, "push", "origin", "HEAD:refs/heads/parallax-live-data")
    base = run_git("-C", seed, "rev-parse", "HEAD").stdout.strip()
    run_git("clone", "--branch", "parallax-live-data", remote, worktree)
    run_git("-C", worktree, "config", "user.name", "Publisher Test")
    run_git("-C", worktree, "config", "user.email", "publisher@example.test")
    for lane in publisher.LANES:
        (worktree / "feeds" / f"{lane}.json").write_text(json.dumps(expected[lane]))
    run_git("-C", worktree, "add", "feeds")
    run_git("-C", worktree, "commit", "-m", "data(parallax): unified customer publish retry")
    stranded = run_git("-C", worktree, "rev-parse", "HEAD").stdout.strip()

    publisher.recover_stranded_commit(worktree, base, stranded, expected)

    remote_head = run_git("--git-dir", remote, "rev-parse", "refs/heads/parallax-live-data").stdout.strip()
    assert remote_head == stranded


def test_stranded_publisher_commit_rejects_unrelated_change(monkeypatch, tmp_path):
    def fake_git(_worktree, *args, **kwargs):
        if args[:2] == ("merge-base", "--is-ancestor"):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "rev-list":
            return subprocess.CompletedProcess(args, 0, "b" * 40 + "\n", "")
        if args[:3] == ("show", "-s", "--format=%s"):
            return subprocess.CompletedProcess(args, 0, "data(parallax): unified customer publish retry\n", "")
        if args[0] == "diff-tree":
            return subprocess.CompletedProcess(args, 0, "README.md\n", "")
        if args[:2] == ("diff", "--name-only"):
            return subprocess.CompletedProcess(args, 0, "README.md\n", "")
        raise AssertionError(args)
    monkeypatch.setattr(publisher, "git", fake_git)
    with pytest.raises(RuntimeError, match="unexpected customer-worktree changes"):
        publisher.recover_stranded_commit(tmp_path, "a" * 40, "b" * 40, {})


def test_stranded_publisher_commit_rejects_remote_movement(monkeypatch, tmp_path):
    feed, db = fixtures(tmp_path)
    expected, _, _ = publisher.build_all(feed, db, NOW)
    calls = []
    def fake_git(_worktree, *args, **_kwargs):
        calls.append(args)
        if args[:2] == ("merge-base", "--is-ancestor"):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "rev-list":
            return subprocess.CompletedProcess(args, 0, "b" * 40 + "\n", "")
        if args[:3] == ("show", "-s", "--format=%s"):
            return subprocess.CompletedProcess(args, 0, "data(parallax): unified customer publish retry\n", "")
        if args[0] == "diff-tree":
            return subprocess.CompletedProcess(args, 0, "feeds/nfl.json\n", "")
        if args[:2] == ("diff", "--name-only"):
            return subprocess.CompletedProcess(args, 0, "feeds/nfl.json\n", "")
        if args[0] == "show":
            lane = args[1].split("feeds/")[-1].removesuffix(".json")
            return subprocess.CompletedProcess(args, 0, json.dumps(expected[lane]), "")
        if args[0] == "ls-remote":
            return subprocess.CompletedProcess(args, 0, "c" * 40 + "\trefs/heads/parallax-live-data\n", "")
        raise AssertionError(args)
    monkeypatch.setattr(publisher, "git", fake_git)
    with pytest.raises(RuntimeError, match="remote branch changed"):
        publisher.recover_stranded_commit(tmp_path, "a" * 40, "b" * 40, expected)
    assert not any(args[0] == "push" for args in calls)


def test_persistent_failure_notification_waits_five_minutes(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    sent = []
    monkeypatch.setattr(publisher, "send_failure_notification", lambda message: sent.append(message) or True)
    first = publisher.failure_health(state, RuntimeError("stale source"), NOW)
    assert sent == []
    publisher.atomic_json(state / "customer_publisher_health.json", first)
    later = publisher.failure_health(state, RuntimeError("stale source"), NOW.replace(minute=36))
    assert len(sent) == 1
    assert "failed for at least 5 minutes" in sent[0]
    assert later["last_failure_alert_at"] == NOW.replace(minute=36).isoformat()


def test_failed_notification_attempt_is_cooled_down(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(publisher, "send_failure_notification", lambda _message: (_ for _ in ()).throw(OSError("offline")))
    prior = publisher.failure_health(state, RuntimeError("stale source"), NOW)
    prior["failure_started_at"] = NOW.replace(minute=20).isoformat()
    publisher.atomic_json(state / "customer_publisher_health.json", prior)
    later = publisher.failure_health(state, RuntimeError("stale source"), NOW)
    assert later["last_failure_alert_at"] == NOW.isoformat()
    assert later["notification_error"] == "OSError"


def test_unchanged_remote_writes_healthy_count_and_sha(monkeypatch, tmp_path):
    feed, db = fixtures(tmp_path)
    worktree, state = tmp_path / "worktree", tmp_path / "state"
    (worktree / "feeds").mkdir(parents=True)
    expected, _, _ = publisher.build_all(feed, db, NOW)
    for lane in publisher.LANES:
        (worktree / "feeds" / f"{lane}.json").write_text(json.dumps(expected[lane]))
    monkeypatch.setattr(publisher, "checked_worktree", lambda _path: "a" * 40)
    calls = []
    def fake_git(_worktree, *args, **_kwargs):
        calls.append(args[0])
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
        if args[0] == "ls-remote":
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\trefs/heads/parallax-live-data\n", "")
        if args[0] == "show":
            lane = args[1].split("/")[-1]
            return subprocess.CompletedProcess(args, 0, (worktree / "feeds" / lane).read_text(), "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(publisher, "git", fake_git)
    health = publisher.publish(worktree, feed, db, state, NOW)
    assert health["state"] == "HEALTHY"
    assert health["remote_head"] == "a" * 40
    assert health["changed_lanes"] == []
    assert all(health["lanes"][lane]["source_buy"] == health["lanes"][lane]["customer_buy"] for lane in publisher.LANES)
    assert "push" not in calls


def test_concurrent_nfl_refresh_does_not_mutate_inflight_publication_snapshot(monkeypatch, tmp_path):
    feed, db = fixtures(tmp_path)
    worktree, state = tmp_path / "worktree", tmp_path / "state"
    (worktree / "feeds").mkdir(parents=True)
    empty = {"schema_version": publisher.CUSTOMER_SCHEMA, "lane": "NFL", "state": "LIVE",
             "summary": {"buy": 0}, "plays": [], "source_feed_generated_at": NOW.isoformat()}
    for lane in publisher.LANES:
        (worktree / "feeds" / f"{lane}.json").write_text(json.dumps({**empty, "lane": lane.upper()}))
    monkeypatch.setattr(publisher, "checked_worktree", lambda _path: "a" * 40)

    def fake_git(_worktree, *args, **_kwargs):
        if args[0] == "rev-parse":
            revision = "b" if args[1] == "HEAD" else "a"
            return subprocess.CompletedProcess(args, 0, revision * 40 + "\n", "")
        if args[0] == "push":
            # The unattended NFL scan atomically replaces the source while this
            # validated one BUY snapshot is completing its Git publication.
            (feed / "nfl.json").write_text(json.dumps(source("nfl", plays=[], buy=0,
                                                               generated_at=(NOW.replace(minute=31)).isoformat())))
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "ls-remote":
            return subprocess.CompletedProcess(args, 0, "b" * 40 + "\trefs/heads/parallax-live-data\n", "")
        if args[0] == "show":
            lane = args[1].split("feeds/")[-1].removesuffix(".json")
            return subprocess.CompletedProcess(args, 0, (worktree / "feeds" / f"{lane}.json").read_text(), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(publisher, "git", fake_git)
    health = publisher.publish(worktree, feed, db, state, NOW)
    published = json.loads((worktree / "feeds/nfl.json").read_text())
    assert health["state"] == "HEALTHY"
    assert health["lanes"]["nfl"]["source_buy"] == health["lanes"]["nfl"]["customer_buy"] == 1
    assert published["plays"][0]["market_id"] == "1083096"
    assert json.loads((feed / "nfl.json").read_text())["summary"]["buy"] == 0


def test_publish_atomically_suppresses_stale_cfb_and_keeps_fresh_lane_parity(monkeypatch, tmp_path):
    feed, db = fixtures(tmp_path)
    (feed / "cfb.json").write_text(json.dumps(source("cfb", plays=[{"action": "BUY"}],
                                                       generated_at="2026-10-07T22:00:00+00:00")))
    expected, _, _ = publisher.build_all(feed, db, NOW)
    worktree, state = tmp_path / "worktree", tmp_path / "state"
    (worktree / "feeds").mkdir(parents=True)
    for lane in publisher.LANES:
        old = dict(expected[lane])
        old.update(state="LIVE", source_schedule_state="COMPLETE", source_feed_generated_at=NOW.isoformat())
        (worktree / "feeds" / f"{lane}.json").write_text(json.dumps(old))
    monkeypatch.setattr(publisher, "checked_worktree", lambda _path: "a" * 40)

    def fake_git(_worktree, *args, **_kwargs):
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
        if args[0] == "ls-remote":
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\trefs/heads/parallax-live-data\n", "")
        if args[0] == "show":
            lane = args[1].split("/")[-1]
            return subprocess.CompletedProcess(args, 0, (worktree / "feeds" / lane).read_text(), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(publisher, "git", fake_git)
    health = publisher.publish(worktree, feed, db, state, NOW)
    customer_cfb = json.loads((worktree / "feeds/cfb.json").read_text())
    assert health["state"] == "DEGRADED"
    assert customer_cfb["state"] == "STALE" and customer_cfb["plays"] == []
    assert health["lanes"]["cfb"]["source_buy"] == 1
    assert health["lanes"]["cfb"]["customer_buy"] == 0
    assert health["lanes"]["cfb"]["parity"] == "STALE_SUPPRESSED"
    for lane in ("nfl", "mlb"):
        assert health["lanes"][lane]["source_state"] == "FRESH"
        assert health["lanes"][lane]["source_buy"] == health["lanes"][lane]["customer_buy"]


def test_deployment_bootstrap_failure_restores_prior_plists_and_loaded_jobs(tmp_path):
    daemons, backups = tmp_path / "daemons", tmp_path / "backups"
    daemons.mkdir()
    before = {}
    for label in deployment.LABELS:
        before[label] = f"prior {label}".encode()
        (daemons / f"{label}.plist").write_bytes(before[label])

    class FakeDeployment(deployment.Deployment):
        def __init__(self):
            super().__init__(daemons, backups)
            self.running = set(deployment.OLD)
            self.fail_new = True
        def preflight(self, *_args):
            pass
        def loaded(self, label):
            return label in self.running
        def bootout(self, label):
            self.running.discard(label)
        def bootstrap(self, label):
            if label == deployment.NEW and self.fail_new:
                self.fail_new = False
                raise RuntimeError("simulated bootstrap failure")
            self.running.add(label)
        def command(self, *args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "", "")

    deploy = FakeDeployment()
    with pytest.raises(RuntimeError, match="simulated bootstrap failure"):
        deploy.apply(tmp_path / "release", "f" * 40, tmp_path / "worktree", "tester")
    assert deploy.running == set(deployment.OLD)
    for label in deployment.LABELS:
        assert (daemons / f"{label}.plist").read_bytes() == before[label]
    assert len(list(backups.glob("*/manifest.json"))) == 1


def test_deployment_preflight_checks_remote_as_configured_user(monkeypatch, tmp_path):
    release, worktree = tmp_path / "release", tmp_path / "customer-worktree"
    (release / "scripts").mkdir(parents=True)
    (release / "scripts" / "parallax_customer_publisher.py").write_text("# release\n")
    sha = "a" * 40
    remote_sha = "b" * 40
    calls = []

    def fake_command(self, *args, check=True):
        calls.append(args)
        output = ""
        if args[0] == "/usr/bin/git" and args[-2:] == ("rev-parse", "HEAD"):
            output = sha + "\n" if str(release) in args else remote_sha + "\n"
        elif args[0] == "/usr/bin/git" and "branch" in args:
            output = "parallax-live-data\n"
        elif args[0] == "/usr/bin/sudo":
            output = remote_sha + "\trefs/heads/parallax-live-data\n"
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(deployment.Deployment, "command", fake_command)
    deployment.Deployment().preflight(release, sha, worktree, "scottsteele")

    assert ("/usr/bin/sudo", "-H", "-u", "scottsteele", "/usr/bin/git", "-C", str(worktree),
            "ls-remote", "--heads", "origin", "parallax-live-data") in calls
    assert not any(args[0] == "/usr/bin/git" and "ls-remote" in args for args in calls)
