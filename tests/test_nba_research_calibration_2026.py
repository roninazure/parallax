"""NBA Phase 1 pre-2026 calibration: offline regression and isolation gates."""
import csv
import hashlib
import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path
import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts/nba_research_calibration_2026.py"
spec = importlib.util.spec_from_file_location("nba_research_calibration_2026", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

FIELDS = ["sport", "game_id", "season", "game_date", "home_team", "away_team", "winner_team"]

def write(path, rows):
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

def fixture(tmp_path):
    hist = tmp_path/"historic.csv"
    recent = tmp_path/"nba-2026.csv"
    rows = []
    for n in range(75):
        day = (date(2025, 1, 1)+timedelta(days=n)).isoformat()
        rows.extend([
            {"sport":"NBA", "game_id":f"h{n}a", "season":2024, "game_date":day,
             "home_team":"CHA", "away_team":"MIA", "winner_team":"CHA" if n%2 else "MIA"},
            {"sport":"NBA", "game_id":f"h{n}b", "season":2024, "game_date":day,
             "home_team":"BOS", "away_team":"NYK", "winner_team":"BOS" if n%3 else "NYK"},
        ])
    write(hist, rows)
    write(recent, [
        {"sport":"NBA", "game_id":"r1", "season":2026, "game_date":"2026-10-20",
         "home_team":"CHA", "away_team":"MIA", "winner_team":"CHA"},
        {"sport":"NBA", "game_id":"r2", "season":2026, "game_date":"2026-10-21",
         "home_team":"BOS", "away_team":"NYK", "winner_team":"NYK"},
    ])
    provenance = recent.with_suffix(".provenance.json")
    provenance.write_text(json.dumps({
        "source": "ESPN_PUBLIC_SCOREBOARD_RESEARCH_ONLY",
        "csv_sha256": hashlib.sha256(recent.read_bytes()).hexdigest()}))
    return hist, recent, provenance

def test_calibration_replay_uses_historical_fit_only(tmp_path):
    hist, recent, provenance = fixture(tmp_path)
    result = module.evaluate(hist, recent, provenance)
    assert result["evaluation_games"] == 2
    assert result["calibration_selected_on_2026"] is False
    assert result["model_certified"] is False
    assert result["raw_fitted_elo"]["n_games"] == 2
    assert result["calibrated_fitted_elo"]["n_games"] == 2
    assert result["buy_authorized"] is False

def test_bad_digest_fails_closed(tmp_path):
    hist, recent, provenance = fixture(tmp_path)
    recent.write_text(recent.read_text() + "\n")
    with pytest.raises(ValueError, match="provenance"):
        module.evaluate(hist, recent, provenance)

def test_rejects_history_eval_overlap(tmp_path):
    hist, recent, provenance = fixture(tmp_path)
    with hist.open("a") as stream:
        stream.write("NBA,overlap,2025,2026-10-20,CHA,MIA,CHA\n")
    with pytest.raises(ValueError, match="overlaps"):
        module.evaluate(hist, recent, provenance)
