"""Research-only 2026 NBA replay and provenance gates."""
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts/nba_research_replay_2026.py"
spec = importlib.util.spec_from_file_location("nba_research_replay_2026", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

FIELDS = ["sport", "game_id", "season", "game_date", "home_team", "away_team", "winner_team"]

def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

def game(identifier, season, day, home, away, winner):
    return dict(sport="NBA", game_id=identifier, season=season, game_date=day,
                home_team=home, away_team=away, winner_team=winner)

def fixture(tmp_path):
    history, current = tmp_path/"history.csv", tmp_path/"current.csv"
    write_csv(history, [game("historic:1", 2025, "2026-04-12", "CHA", "MIA", "CHA")])
    write_csv(current, [
        game("espn:1", 2026, "2026-10-24", "CHA", "MIA", "CHA"),
        game("espn:2", 2026, "2026-10-24", "BOS", "NYK", "BOS")])
    provenance = current.with_suffix(".provenance.json")
    provenance.write_text(json.dumps({
        "source": "ESPN_PUBLIC_SCOREBOARD_RESEARCH_ONLY",
        "csv_sha256": hashlib.sha256(current.read_bytes()).hexdigest()}))
    return history, current, provenance

def test_chronological_replay_research_only(tmp_path):
    history, current, provenance = fixture(tmp_path)
    result = module.evaluate(history, current, provenance)
    assert result["n_games"] == 2
    assert result["same_day_results_excluded"] is True
    assert result["model_changed"] is False
    assert result["buy_authorized"] is False

def test_tampered_snapshot_fails_closed(tmp_path):
    history, current, provenance = fixture(tmp_path)
    current.write_text(current.read_text() + "\n")
    with pytest.raises(ValueError, match="provenance"):
        module.evaluate(history, current, provenance)

def test_duplicate_current_fixture_fails_closed(tmp_path):
    history, current, provenance = fixture(tmp_path)
    with current.open("a") as stream:
        stream.write("NBA,espn:3,2026,2026-10-24,CHA,MIA,MIA\n")
    raw = json.loads(provenance.read_text())
    raw["csv_sha256"] = hashlib.sha256(current.read_bytes()).hexdigest()
    provenance.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="Duplicate"):
        module.evaluate(history, current, provenance)
