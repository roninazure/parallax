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

def test_duplicate_fails_closed():
    with pytest.raises(ValueError, match="duplicate"):
        module.snapshot(date(2026,10,20), date(2026,10,20),
                        fetch=lambda _: {"events": [game(), game(id="99")]})

def test_invalid_window_or_payload_fails_closed():
    with pytest.raises(ValueError):
        module.snapshot(date(2026,10,21), date(2026,10,20), fetch=lambda _: {"events":[]})
    with pytest.raises(ValueError):
        module.snapshot(date(2026,10,20), date(2026,10,20), fetch=lambda _: {})
