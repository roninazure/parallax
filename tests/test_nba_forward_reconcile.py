"""Network-free NBA Phase 1 reconciliation regression tests."""
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts/nba_forward_reconcile.py"
spec = importlib.util.spec_from_file_location("nba_forward_reconcile", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def snapshot(directory, name, stamp, screened):
    (directory / name).write_text(json.dumps({"captured_at_utc": stamp, "screened": screened}))

def row(p=.7, kickoff="2026-11-05T23:00:00+00:00", matchup="MIA@CHA"):
    return {"raw_elo_home_probability": p, "start_utc": kickoff, "matchup": matchup}

def final(home="CHA", away="MIA", home_score="120", away_score="110"):
    return {"events": [{"id": "123", "status": {"type": {"completed": True}},
        "competitions": [{"competitors": [
            {"homeAway": "home", "team": {"abbreviation": home}, "score": home_score},
            {"homeAway": "away", "team": {"abbreviation": away}, "score": away_score},
        ]}]}]}

def test_earliest_frozen_and_posttipoff_excluded(tmp_path):
    snapshot(tmp_path, "nba-shadow-a.json", "2026-11-05T10:00:00Z", [row(.7)])
    snapshot(tmp_path, "nba-shadow-b.json", "2026-11-05T12:00:00Z", [row(.9)])
    snapshot(tmp_path, "nba-shadow-c.json", "2026-11-05T23:00:00Z", [row(.1)])
    forecasts, counts = module.archived_forecasts(tmp_path)
    assert len(forecasts) == 1
    assert next(iter(forecasts.values()))["home_probability"] == .7
    assert counts["post_tipoff_rows"] == 1

def test_grading_brier_ece_and_no_buy(tmp_path):
    snapshot(tmp_path, "nba-shadow-a.json", "2026-11-05T10:00:00Z", [row()])
    forecasts, _ = module.archived_forecasts(tmp_path)
    report = module.evaluate(forecasts, now=datetime(2026, 11, 6, tzinfo=UTC),
                             fetch=lambda _: final())
    assert report["graded_games"] == 1
    assert report["accuracy"] == 1
    assert report["brier"] == pytest.approx(.09)
    assert report["ece_10_bins"] == pytest.approx(.3)
    assert report["buy_authorized"] is False

def test_future_vs_missing_and_team_alias(tmp_path):
    snapshot(tmp_path, "nba-shadow-a.json", "2026-11-05T10:00:00Z",
             [row(), row(kickoff="2026-11-07T23:00:00Z", matchup="GS@NY")])
    forecasts, _ = module.archived_forecasts(tmp_path)
    report = module.evaluate(forecasts, now=datetime(2026, 11, 6, tzinfo=UTC),
                             fetch=lambda _: {"events": []})
    assert report["unmatched_counts"] == {"FUTURE": 1, "MISSING_FINAL_OR_IDENTITY": 1}
    assert any(r["matchup"] == "GSW@NYK" for r in report["unmatched"])

def test_rejects_invalid_probability_and_naive_timestamp(tmp_path):
    snapshot(tmp_path, "nba-shadow-a.json", "2026-11-05T10:00:00Z", [row(float("nan"))])
    forecasts, counts = module.archived_forecasts(tmp_path)
    assert not forecasts
    assert counts["invalid_rows"] == 1
    with pytest.raises(ValueError):
        module.parse_stamp("2026-11-05T10:00:00")

def test_washington_espn_alias_matches_archived_forecast(tmp_path):
    snapshot(tmp_path, "nba-shadow-a.json", "2026-11-04T10:00:00Z",
             [row(.64, kickoff="2026-11-04T23:00:00Z", matchup="ATL@WAS")])
    forecasts, _ = module.archived_forecasts(tmp_path)
    report = module.evaluate(
        forecasts, now=datetime(2026, 11, 5, tzinfo=UTC),
        fetch=lambda _: final(home="WSH", away="ATL", home_score="105", away_score="120"))
    assert report["graded_games"] == 1
    assert report["unmatched_counts"] == {}
    assert report["games"][0]["matchup"] == "ATL@WAS"
    assert report["correct"] == 0
