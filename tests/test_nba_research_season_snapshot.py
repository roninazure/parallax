"""Offline tests for bounded NBA research finals acquisition."""
import importlib.util
import sys
from datetime import date
from pathlib import Path
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("nba_research_season_snapshot",
                                              SCRIPTS / "nba_research_season_snapshot.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def game(id="12", kind=2, done=True, home="NY", away="GS", hs="118", ans="110"):
    return {"id": id, "season": {"type": kind},
            "status": {"type": {"completed": done}},
            "competitions": [{"competitors": [
                {"homeAway": "home", "team": {"abbreviation": home}, "score": hs},
                {"homeAway": "away", "team": {"abbreviation": away}, "score": ans}]}]}

def test_regular_completed_only():
    def fetch(day):
        return {"events": [game(), game(id="13", kind=1), game(id="14", done=False)]} if day == "20261020" else {"events": []}
    rows, provenance = module.snapshot(date(2026,10,20), date(2026,10,21), fetch=fetch)
    assert len(rows) == 1
    assert rows[0]["home_team"] == "NYK" and rows[0]["away_team"] == "GSW"
    assert rows[0]["winner_team"] == "NYK"
    assert provenance["dates_requested"] == 2
    assert provenance["independent_source_verified"] is False
    assert provenance["expected_schedule_reconciled"] is False
    assert provenance["all_days_provider_internal_complete"] is False
    assert provenance["daily_counts"][0] == {
        "day": "20261020", "finals": 1, "provider_regular_events": 2,
        "unresolved_events": 1, "provider_internal_complete": False,
    }
    assert provenance["daily_counts"][1]["provider_internal_complete"] is True

def test_duplicate_fails_closed():
    with pytest.raises(ValueError, match="duplicate"):
        module.snapshot(date(2026,10,20), date(2026,10,20),
                        fetch=lambda _: {"events": [game(), game(id="99")]})

def test_invalid_window_or_payload_fails_closed():
    with pytest.raises(ValueError):
        module.snapshot(date(2026,10,21), date(2026,10,20), fetch=lambda _: {"events":[]})
    with pytest.raises(ValueError):
        module.snapshot(date(2026,10,20), date(2026,10,20), fetch=lambda _: {})


def test_unknown_completion_status_fails_closed():
    event = game()
    event["status"]["type"].pop("completed")
    with pytest.raises(ValueError, match="completion status"):
        module.snapshot(date(2026,10,20), date(2026,10,20),
                        fetch=lambda _: {"events": [event]})


def test_duplicate_provider_event_id_fails_closed():
    with pytest.raises(ValueError, match="duplicate event identity"):
        module.snapshot(date(2026,10,20), date(2026,10,20),
                        fetch=lambda _: {"events": [game(), game(home="BOS", away="MIA")]})
