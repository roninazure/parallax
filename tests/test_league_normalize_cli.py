"""CLI smoke test only; no network and no production files."""
import json
import os
import subprocess
import sys
from pathlib import Path


def test_cli_normalizes_saved_nba_game_and_refuses_unscoped_input(tmp_path):
    sample = {"data": [{
        "id": 100, "season": 2025, "date": "2025-10-20",
        "status_state": "final", "postseason": False,
        "home_team": {"abbreviation": "BOS"},
        "visitor_team": {"abbreviation": "NYK"},
        "home_team_score": 101, "visitor_team_score": 99,
    }]}
    src, dst = tmp_path / "nba.json", tmp_path / "finals.csv"
    src.write_text(json.dumps(sample))
    root = Path(__file__).resolve().parents[1]
    cmd = [sys.executable, str(root / "scripts/nba_nhl_normalize_snapshots.py"),
           "--nba-json", str(src), "--output", str(dst)]
    env = dict(os.environ, PYTHONPATH=str(root))
    failed = subprocess.run(cmd, capture_output=True, text=True, cwd=root, env=env)
    assert failed.returncode != 0
    assert not dst.exists()
    ok = subprocess.run(cmd + ["--nba-regular-query-verified"],
                        capture_output=True, text=True, cwd=root, env=env)
    assert ok.returncode == 0, ok.stderr
    assert "OFFLINE_EXPERIMENTAL_ONLY" in ok.stdout
    assert "NBA,bdl:100,2025" in dst.read_text()
