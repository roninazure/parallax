"""Network-free NBA schedule completeness and fail-closed provenance tests."""
import csv
import hashlib
import json
from pathlib import Path

import pytest

from scripts.nba_schedule_completeness_audit import audit


def sources(tmp_path, *, winner="BOS"):
    espn, prov, bdl = (tmp_path / name for name in
                       ("espn.csv", "espn.provenance.json", "bdl.json"))
    with espn.open("w", newline="") as stream:
        w = csv.DictWriter(stream, fieldnames=[
            "sport", "game_id", "season", "game_date", "home_team", "away_team", "winner_team"])
        w.writeheader()
        w.writerow({"sport": "NBA", "game_id": "espn:1", "season": 2026,
                    "game_date": "2026-10-22", "home_team": "BOS",
                    "away_team": "NYK", "winner_team": winner})
    prov.write_text(json.dumps({"csv_sha256": hashlib.sha256(espn.read_bytes()).hexdigest(),
                                "source": "ESPN_PUBLIC_SCOREBOARD_RESEARCH_ONLY"}))
    bdl.write_text(json.dumps({"data": [{
        "date": "2026-10-22", "season": 2026, "status_state": "final",
        "postseason": False, "postponed": False,
        "home_team": {"abbreviation": "BOS"}, "visitor_team": {"abbreviation": "NYK"},
        "home_team_score": 105, "visitor_team_score": 90}]}))
    return espn, prov, bdl


def schedule(tmp_path, entries, *, tamper=False):
    path, prov = tmp_path/"schedule.csv", tmp_path/"schedule.provenance.json"
    with path.open("w", newline="") as stream:
        w = csv.DictWriter(stream, fieldnames=[
            "game_date", "home_team", "away_team", "status"])
        w.writeheader()
        w.writerows(entries)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    prov.write_text(json.dumps({
        "csv_sha256": "wrong" if tamper else sha,
        "schedule_source": "INDEPENDENT_TEST_FIXTURE",
        "coverage_start": "2026-10-22", "coverage_end": "2026-10-23",
        "source_authorization_verified": True}))
    return path, prov


GAME = {"game_date": "2026-10-22", "home_team": "BOS",
        "away_team": "NYK", "status": "FINAL"}


def test_missing_schedule_cannot_be_certified(tmp_path):
    result = audit(*sources(tmp_path))
    assert result["status"] == "SCHEDULE_SOURCE_REQUIRED"
    assert result["network_requests"] == 0
    assert result["full_schedule_coverage_verified"] is False
    assert result["buy_authorized"] is False


def test_count_match_is_not_independent_authorization(tmp_path):
    original = sources(tmp_path)
    result = audit(*original, *schedule(tmp_path, [GAME]))
    assert result["fixture_coverage_complete"] is True
    assert result["status"] == "RESEARCH_FIXTURE_COUNTS_MATCH_UNVERIFIED"
    assert result["source_authorization_verified"] is False
    assert result["full_schedule_coverage_verified"] is False
    assert result["model_certified"] is False
    assert result["network_requests"] == 0


def test_missing_final_reports_exact_fixture(tmp_path):
    original = sources(tmp_path)
    extra = {"game_date": "2026-10-23", "home_team": "LAL",
             "away_team": "GSW", "status": "FINAL"}
    result = audit(*original, *schedule(tmp_path, [GAME, extra]))
    assert result["status"] == "RESEARCH_SCHEDULE_RECONCILIATION_BLOCKED"
    assert result["missing_finals"] == [{"date": "2026-10-23", "away": "GSW", "home": "LAL"}]
    assert result["fixture_coverage_complete"] is False


def test_postponed_unresolved_not_counted_as_final(tmp_path):
    original = sources(tmp_path)
    delayed = {"game_date": "2026-10-23", "home_team": "LAL",
               "away_team": "GSW", "status": "POSTPONED"}
    result = audit(*original, *schedule(tmp_path, [GAME, delayed]))
    assert result["postponed"] == 1
    assert result["final_fixtures"] == 1
    assert len(result["nonfinal_fixtures"]) == 1
    assert result["fixture_coverage_complete"] is True
    assert result["full_schedule_coverage_verified"] is False


def test_duplicate_and_tampered_provenance_block(tmp_path):
    original = sources(tmp_path)
    result = audit(*original, *schedule(tmp_path, [GAME, GAME]))
    assert len(result["duplicates"]) == 1
    assert result["fixture_coverage_complete"] is False
    schedule(tmp_path, [GAME], tamper=True)
    with pytest.raises(ValueError, match="hash mismatch"):
        audit(*original, tmp_path/"schedule.csv", tmp_path/"schedule.provenance.json")
