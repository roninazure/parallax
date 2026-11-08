"""No-network cross-source NBA daily reconciliation regression tests."""
import csv
import hashlib
import json
import sys

import pytest

from scripts import nba_daily_cross_source_reconcile as module
from scripts.nba_daily_cross_source_reconcile import compare


def inputs(tmp_path, *, espn_winner="BOS"):
    espn = tmp_path / "finals.csv"
    with espn.open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=["sport", "game_id", "season", "game_date", "home_team", "away_team", "winner_team"])
        writer.writeheader()
        writer.writerow(dict(sport="NBA", game_id="espn:1", season=2026, game_date="2026-10-22", home_team="BOS", away_team="NYK", winner_team=espn_winner))
    provenance = tmp_path / "finals.provenance.json"
    provenance.write_text(json.dumps({"source": "ESPN_PUBLIC_SCOREBOARD_RESEARCH_ONLY", "csv_sha256": hashlib.sha256(espn.read_bytes()).hexdigest()}))
    bdl = tmp_path / "bdl.json"
    bdl.write_text(json.dumps({"data": [dict(date="2026-10-22", season=2026, status_state="final", postseason=False, postponed=False,
        home_team={"abbreviation": "BOS"}, visitor_team={"abbreviation": "NYK"}, home_team_score=105, visitor_team_score=90)]}))
    return espn, provenance, bdl


def test_exact_outcome_and_identity_reconciles_without_certification(tmp_path):
    result = compare(*inputs(tmp_path))
    assert result["status"] == "RESEARCH_SOURCES_MATCH"
    assert result["expected_games"] == result["observed_games"] == 1
    assert result["daily"][0]["matched"] is True
    assert result["full_schedule_coverage_verified"] is False
    assert result["buy_authorized"] is False


def test_disagreed_winner_blocks(tmp_path):
    result = compare(*inputs(tmp_path, espn_winner="NYK"))
    assert result["status"] == "RESEARCH_RECONCILIATION_BLOCKED"
    assert len(result["winner_disagreements"]) == 1
    assert result["daily"][0]["matched"] is False


def test_write_once_output_refuses_overwrite(tmp_path, monkeypatch):
    espn, provenance, bdl = inputs(tmp_path)
    target = tmp_path / "evidence.json"
    monkeypatch.setattr(sys, "argv", ["audit", "--espn-csv", str(espn),
        "--espn-provenance", str(provenance), "--bdl-json", str(bdl),
        "--output", str(target)])
    assert module.main() == 0
    result = json.loads(target.read_text())
    assert result["cross_source_results_consistent"] is True
    assert result["model_certified"] is False
    with pytest.raises(SystemExit):
        module.main()
