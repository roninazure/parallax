"""Offline NBA unattended reconciliation safety and idempotency tests."""
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts/nba_research_daily_reconcile.py"
spec = importlib.util.spec_from_file_location("nba_research_daily_reconcile", MODULE)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
NOW = datetime(2026, 11, 5, 17, 30, tzinfo=UTC)

def sample(snapshots):
    (snapshots / "nba-shadow-test.json").write_text(json.dumps({
        "captured_at_utc": "2026-11-04T10:00:00Z",
        "screened": [{"matchup":"MIA@CHA", "start_utc":"2026-11-04T23:00:00Z",
                      "raw_elo_home_probability": 0.7}]}))

def grade(forecasts, *, now):
    assert len(forecasts) == 1
    return {"scoreboard_errors": {}, "graded_games": 1, "unique_forecasts": 1,
            "correct": 1, "accuracy": 1.0, "brier": .09, "ece_10_bins": .3,
            "unmatched": [], "unmatched_counts": {}, "buy_authorized": False}

def test_daily_once_no_duplicate_network(tmp_path):
    snapshots, state = tmp_path/"snapshots", tmp_path/"state"
    snapshots.mkdir()
    sample(snapshots)
    calls = []
    def scorer(forecasts, *, now):
        calls.append(now)
        return grade(forecasts, now=now)
    result = runner.run(snapshots=snapshots, state=state, now=NOW, scorer=scorer)
    assert result["status"] == "COMPLETE_FOR_COMPLETED_MATCHES"
    assert result["graded_games"] == 1
    assert len(calls) == 1
    assert runner.run(snapshots=snapshots, state=state, now=NOW, scorer=scorer)["status"] == "SKIPPED_ALREADY_ATTEMPTED"
    assert len(calls) == 1
    report = json.loads((state/"scorecard-20261105.json").read_text())
    assert report["buy_authorized"] is False
    assert (state/"scorecard-20261105.json").stat().st_mode & 0o777 == 0o600
    assert state.stat().st_mode & 0o777 == 0o700

def test_error_reserves_attempt_and_never_publishes_report(tmp_path):
    snapshots, state = tmp_path/"snapshots", tmp_path/"state"
    snapshots.mkdir()
    sample(snapshots)
    def broken(forecasts, *, now):
        raise RuntimeError("scoreboard unavailable")
    with pytest.raises(RuntimeError, match="scoreboard unavailable"):
        runner.run(snapshots=snapshots, state=state, now=NOW, scorer=broken)
    assert (state/"attempt-20261105.json").exists()
    assert not (state/"scorecard-20261105.json").exists()
    assert runner.run(snapshots=snapshots, state=state, now=NOW, scorer=broken)["status"] == "SKIPPED_ALREADY_ATTEMPTED"

def test_missing_final_is_explicit_partial(tmp_path):
    snapshots, state = tmp_path/"snapshots", tmp_path/"state"
    snapshots.mkdir()
    sample(snapshots)
    def partial(forecasts, *, now):
        return {"scoreboard_errors": {}, "graded_games": 1, "unique_forecasts": 2,
                "correct": 1, "accuracy": 1, "brier": .1, "ece_10_bins": .2,
                "unmatched": [{"matchup":"ATL@WAS"}],
                "unmatched_counts": {"MISSING_FINAL_OR_IDENTITY": 1},
                "buy_authorized": False}
    result = runner.run(snapshots=snapshots, state=state, now=NOW, scorer=partial)
    assert result["status"] == "PARTIAL_MISSING_FINAL_OR_IDENTITY"

def test_rejects_invalid_snapshot_without_reserving_day(tmp_path):
    snapshots, state = tmp_path/"snapshots", tmp_path/"state"
    snapshots.mkdir()
    (snapshots/"nba-shadow-bad.json").write_text("{}")
    with pytest.raises(ValueError):
        runner.run(snapshots=snapshots, state=state, now=NOW, scorer=grade)
    assert not list(state.glob("attempt-*.json"))

def test_request_ceiling_fails_before_attempt_or_network(tmp_path):
    snapshots, state = tmp_path/"snapshots", tmp_path/"state"
    snapshots.mkdir()
    sample(snapshots)
    (snapshots/"nba-shadow-day-two.json").write_text(json.dumps({
        "captured_at_utc": "2026-11-03T10:00:00Z",
        "screened": [{"matchup": "BOS@NYK", "start_utc": "2026-11-03T23:00:00Z",
                      "raw_elo_home_probability": 0.55}]}))
    def must_not_call(*args, **kwargs):
        pytest.fail("request ceiling must prevent scorer network calls")
    with pytest.raises(ValueError, match="request ceiling exceeded"):
        runner.run(snapshots=snapshots, state=state, now=NOW, scorer=must_not_call)
    assert not list(state.glob("attempt-*.json"))


def test_cached_cross_source_uses_zero_requests_and_write_once(tmp_path, monkeypatch):
    import csv
    import hashlib
    snapshots, state = tmp_path/"snapshots", tmp_path/"state"
    snapshots.mkdir()
    sample(snapshots)
    espn, prov, bdl = tmp_path/"finals.csv", tmp_path/"finals.provenance.json", tmp_path/"bdl.json"
    with espn.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "sport", "game_id", "season", "game_date", "home_team", "away_team", "winner_team"])
        writer.writeheader()
        writer.writerow(dict(sport="NBA", game_id="espn:1", season=2026,
                             game_date="2026-11-04", home_team="CHA",
                             away_team="MIA", winner_team="CHA"))
    prov.write_text(json.dumps({"source": "ESPN_PUBLIC_SCOREBOARD_RESEARCH_ONLY",
                                "csv_sha256": hashlib.sha256(espn.read_bytes()).hexdigest()}))
    bdl.write_text(json.dumps({"data": [{
        "date": "2026-11-04", "season": 2026, "status_state": "final",
        "postseason": False, "postponed": False, "home_team": {"abbreviation": "CHA"},
        "visitor_team": {"abbreviation": "MIA"},
        "home_team_score": 120, "visitor_team_score": 110}]}))
    def must_not_call(*args, **kwargs):
        pytest.fail("cached inputs must never invoke live scorer")
    result = runner.run(snapshots=snapshots, state=state, now=NOW, scorer=must_not_call,
                        cached_sources=(espn, prov, bdl))
    assert result["status"] == "CACHED_CROSS_SOURCE_RESEARCH_ONLY"
    assert result["network_requests"] == 0
    report = json.loads(Path(result["report"]).read_text())
    assert report["evidence"]["expected_games"] == 1
    assert report["buy_authorized"] is False
    assert runner.run(snapshots=snapshots, state=state, now=NOW, scorer=must_not_call,
                      cached_sources=(espn, prov, bdl))["status"] == "SKIPPED_ALREADY_ATTEMPTED"


def test_invalid_cached_evidence_fails_closed(tmp_path):
    snapshots, state = tmp_path/"snapshots", tmp_path/"state"
    snapshots.mkdir()
    sample(snapshots)
    files = tuple(tmp_path/name for name in ("x.csv", "x.json", "y.json"))
    for file in files:
        file.write_text("{}")
    with pytest.raises((ValueError, KeyError)):
        runner.run(snapshots=snapshots, state=state, now=NOW,
                   cached_sources=files)
    assert list(state.glob("attempt-*.json"))
    assert not list(state.glob("scorecard-*.json"))
