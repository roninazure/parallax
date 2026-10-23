"""Offline safety checks for the opt-in NHL shadow launchd job and snapshots."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
import json
import plistlib
import sys

from scripts import nhl_shadow_slate_research as scan
from parallax.league_shadow_evidence import ShadowFixture


JOB = Path(__file__).resolve().parents[1] / "ops/launchd/com.swarmaxis.parallax-nhl-shadow-research.plist"


def test_launchd_job_is_isolated_and_hourly():
    with JOB.open("rb") as stream:
        plist = plistlib.load(stream)

    assert plist["Label"] == "com.swarmaxis.parallax-nhl-shadow-research"
    assert plist["UserName"] == "scottsteele"  # Non-root system LaunchDaemon
    assert plist["StartInterval"] == 3600
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is False
    assert plist["WorkingDirectory"] == (
        "/Users/scottsteele/parallax-research/parallax-shadow"
    )
    assert plist["ProgramArguments"] == [
        "/Users/scottsteele/parallax-research/venv312/bin/python",
        "-m", "scripts.nhl_shadow_slate_research",
        "--max-games", "2", "--write-research-snapshot",
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
