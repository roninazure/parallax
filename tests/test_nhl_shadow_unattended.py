"""Offline safety checks for the opt-in NHL shadow launchd job and snapshots."""
from datetime import UTC, datetime, timedelta
import fcntl
import os
import urllib.error

import pytest
from pathlib import Path
import json
import plistlib
import sys

from scripts import nhl_shadow_slate_research as scan
from parallax.league_shadow_evidence import ShadowFixture


JOB = Path(__file__).resolve().parents[1] / "ops/launchd/com.swarmaxis.parallax-nhl-shadow-research.plist"


def test_launchd_job_is_isolated_with_conservative_cadence():
    with JOB.open("rb") as stream:
        plist = plistlib.load(stream)

    assert plist["Label"] == "com.swarmaxis.parallax-nhl-shadow-research"
    assert plist["UserName"] == "scottsteele"  # Non-root system LaunchDaemon
    assert plist["StartInterval"] == 7200
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is False
    assert plist["WorkingDirectory"] == (
        "/Users/scottsteele/parallax-research/parallax-shadow"
    )
    assert plist["ProgramArguments"] == [
        "/Users/scottsteele/parallax-research/venv312/bin/python",
        "-m", "scripts.nhl_shadow_slate_research",
        "--max-games", "2", "--write-research-snapshot", "--summary-only",
    ]
    assert plist["EnvironmentVariables"]["PYTHONPATH"].endswith(
        "/parallax-research/parallax-shadow"
    )
    assert all(
        "/SwarmEdge/releases/" not in str(value)
        and "/swarm-runtime/swarm-edge" not in str(value)
        for value in plist.values()
    )


def test_run_writes_immutable_forward_shadow_snapshot_offline(
    monkeypatch, tmp_path, capsys
):
    """A scheduled run archives prospective, non-BUY evaluations before start."""
    now = datetime.now(UTC)
    fixture = ShadowFixture(
        sport="NHL", game_id="nhl:42", season=2026,
        kickoff=now + timedelta(days=2),
        home_team="CGY", away_team="TOR",
        venue_home_name="Calgary", venue_away_name="Toronto",
    )
    ticker = "KXNHLGAME-TEST"
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return {"gameWeek": []}

    monkeypatch.setattr(scan, "fetch_json", fake_fetch)
    monkeypatch.setattr(scan, "nhl_fixtures", lambda schedule, clock: {ticker: fixture})
    monkeypatch.setattr(scan, "kalshi_events", lambda: ({ticker: {"event_ticker": ticker}}, 1))
    monkeypatch.setattr(scan, "load_history", lambda directory: [])
    monkeypatch.setattr(
        scan, "evaluate_fixture",
        lambda f, e, history: {
            "game_id": f.game_id,
            "matchup": "TOR@CGY",
            "start_utc": f.kickoff.isoformat(),
            "positions": [
                {"action": "WATCH", "side": "YES"},
                {"action": "PASS", "side": "NO"},
            ],
        },
    )
    monkeypatch.setattr(scan, "RESEARCH_OUTPUT", tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "nhl_shadow_slate_research.py", "--max-games", "2",
        "--write-research-snapshot",
    ])
    scan.main()
    output = capsys.readouterr().out
    snapshots = sorted(tmp_path.glob("nhl-shadow-*.json"))
    assert len(snapshots) == 1
    assert "NO ORDERS / NO PUBLICATION / NO ALERTS" in output
    doc = json.loads(snapshots[0].read_text())
    assert doc["mode"] == "RESEARCH_ONLY"
    assert doc["buy_authorized"] is False
    assert doc["publish_authorized"] is False
    assert doc["orders_authorized"] is False
    assert doc["request_budget_max"] == 8
    assert doc["screened_count"] == 1
    assert doc["screened"][0]["positions"][0]["action"] == "WATCH"
    assert len(fetched) == 1



def test_research_guard_prevents_restarts_inside_cooldown(monkeypatch, tmp_path):
    monkeypatch.setattr(scan, "GUARD_DIR", tmp_path)
    t = datetime(2026, 10, 23, 12, tzinfo=UTC)
    with scan.guarded_run(now=t):
        pass
    with pytest.raises(scan.ResearchRunSkipped, match="minimum spacing"):
        with scan.guarded_run(now=t + timedelta(hours=1)):
            raise AssertionError("guard should reject before network")
    with scan.guarded_run(now=t + timedelta(hours=2)):
        pass
    doc = json.loads((tmp_path / ".nhl-shadow-budget.json").read_text())
    assert doc["runs_today"] == 2
    assert scan.MAX_GAMES == 2
    assert scan.MAX_EVENT_PAGES == 3


def test_research_guard_blocks_daily_budget_and_fails_closed_on_corruption(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(scan, "GUARD_DIR", tmp_path)
    t = datetime(2026, 10, 23, 12, tzinfo=UTC)
    budget = tmp_path / ".nhl-shadow-budget.json"
    scan._write_guard_state(budget, {
        "utc_day": t.date().isoformat(),
        "runs_today": scan.MAX_RUNS_PER_UTC_DAY,
    })
    with pytest.raises(scan.ResearchRunSkipped, match="daily research"):
        with scan.guarded_run(now=t):
            raise AssertionError("must not run")
    budget.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(scan.ResearchRunSkipped, match="unreadable request-budget"):
        with scan.guarded_run(now=t):
            raise AssertionError("must fail closed")


def test_research_guard_blocks_simultaneous_instance(monkeypatch, tmp_path):
    monkeypatch.setattr(scan, "GUARD_DIR", tmp_path)
    lock_file = tmp_path / ".nhl-shadow.lock"
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(scan.ResearchRunSkipped, match="another NHL research"):
            with scan.guarded_run():
                raise AssertionError("must reject overlap")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_research_guard_cools_down_on_provider_denial(monkeypatch, tmp_path):
    monkeypatch.setattr(scan, "GUARD_DIR", tmp_path)
    t = datetime.now(UTC)
    with pytest.raises(urllib.error.HTTPError) as denied:
        with scan.guarded_run(now=t):
            raise urllib.error.HTTPError("https://example.test", 429, "Too Many Requests", {}, None)
    assert denied.value.code == 429
    doc = json.loads((tmp_path / ".nhl-shadow-budget.json").read_text())
    assert datetime.fromisoformat(doc["cooldown_until_utc"]) >= t + timedelta(hours=23)
    with pytest.raises(scan.ResearchRunSkipped, match="provider cooldown"):
        with scan.guarded_run(now=t + timedelta(hours=2)):
            raise AssertionError("must not retry denied provider")
