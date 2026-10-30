"""Synthetic-only checks for read-only league results reconciliation."""

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.reconcile_league_results import reconcile_files


COLUMNS = (
    "sport", "game_id", "season", "game_date",
    "home_team", "away_team", "winner_team",
)


def _rows(sport: str, prefix: str) -> list[dict[str, str]]:
    return [
        {
            "sport": sport,
            "game_id": f"{prefix}-{i}",
            "season": "2025",
            "game_date": f"2025-10-{i + 1:02d}",
            "home_team": f"H{i}",
            "away_team": f"A{i}",
            "winner_team": f"H{i}" if i % 2 else f"A{i}",
        }
        for i in range(4)
    ]


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize("sport", ["NBA", "NHL"])
def test_reconciles_distinct_provider_ids_and_order_but_never_certifies(tmp_path, sport):
    primary, secondary = tmp_path / "primary.csv", tmp_path / "secondary.csv"
    _write(primary, _rows(sport, "a"))
    _write(secondary, list(reversed(_rows(sport, "b"))))
    report = reconcile_files(primary=primary, secondary=secondary, sport=sport)
    assert report["status"] == "FIXTURES_RECONCILED"
    assert report["games_reconciled"] == 4
    assert len(report["reconciled_fixture_sha256"]) == 64
    assert len(report["primary_history_fingerprint_sha256"]) == 64
    reversed_report = reconcile_files(primary=secondary, secondary=primary, sport=sport)
    assert reversed_report["reconciled_fixture_sha256"] == report["reconciled_fixture_sha256"]
    assert reversed_report["primary_history_fingerprint_sha256"] != report["primary_history_fingerprint_sha256"]
    assert report["primary_source_sha256"] != report["secondary_source_sha256"]
    assert report["provider_independence_verified"] is False
    assert report["source_authorization_verified"] is False
    assert report["forward_holdout_verified"] is False
    assert report["model_certified"] is False
    assert report["production_eligible"] is False


def test_identical_source_bytes_are_not_independent_evidence(tmp_path):
    primary, secondary = tmp_path / "primary.csv", tmp_path / "secondary.csv"
    _write(primary, _rows("NBA", "a"))
    secondary.write_bytes(primary.read_bytes())
    with pytest.raises(ValueError, match="identical SHA-256"):
        reconcile_files(primary=primary, secondary=secondary, sport="NBA")


@pytest.mark.parametrize("mutation", ["missing", "winner", "duplicate"])
def test_missing_wrong_winner_and_duplicate_fixture_fail_closed(tmp_path, mutation):
    primary, secondary = tmp_path / "primary.csv", tmp_path / "secondary.csv"
    _write(primary, _rows("NBA", "a"))
    rows = _rows("NBA", "b")
    if mutation == "missing":
        rows.pop()
    elif mutation == "winner":
        rows[0]["winner_team"] = "H0"
    else:
        rows.append({**rows[0], "game_id": "another-provider-id"})
    _write(secondary, rows)
    with pytest.raises(ValueError, match="reconcile|duplicate"):
        reconcile_files(primary=primary, secondary=secondary, sport="NBA")


def test_selected_league_must_be_present_in_both_files(tmp_path):
    primary, secondary = tmp_path / "primary.csv", tmp_path / "secondary.csv"
    _write(primary, _rows("NBA", "a"))
    _write(secondary, _rows("NHL", "b"))
    with pytest.raises(ValueError, match="NBA results missing"):
        reconcile_files(primary=primary, secondary=secondary, sport="NBA")


def test_cli_exit_codes_and_machine_readable_success(tmp_path):
    primary, secondary = tmp_path / "primary.csv", tmp_path / "secondary.csv"
    _write(primary, _rows("NBA", "a"))
    _write(secondary, list(reversed(_rows("NBA", "b"))))
    script = Path(__file__).resolve().parents[1] / "scripts/reconcile_league_results.py"
    command = [
        sys.executable, str(script), "--sport", "NBA",
        "--primary-history", str(primary),
        "--independent-history", str(secondary),
    ]
    success = subprocess.run(command, capture_output=True, text=True, check=False)
    assert success.returncode == 0, success.stderr
    assert json.loads(success.stdout)["status"] == "FIXTURES_RECONCILED"
    _write(secondary, _rows("NBA", "b")[:-1])
    failure = subprocess.run(command, capture_output=True, text=True, check=False)
    assert failure.returncode == 1
    assert "RECONCILIATION_FAILED" in failure.stderr
    assert not failure.stdout


@pytest.mark.parametrize("changed_source", ["primary", "secondary"])
def test_source_modified_during_parse_fails_closed(tmp_path, monkeypatch, changed_source):
    """Report hashes must describe the exact source bytes parsed."""
    from scripts import reconcile_league_results as reconciliation

    primary, secondary = tmp_path / "primary.csv", tmp_path / "secondary.csv"
    _write(primary, _rows("NBA", "a"))
    _write(secondary, _rows("NBA", "b"))
    original_loader = reconciliation.load_final_games_csv

    def mutating_loader(path):
        games = original_loader(path)
        if path == (primary if changed_source == "primary" else secondary):
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\\n")
        return games

    monkeypatch.setattr(reconciliation, "load_final_games_csv", mutating_loader)
    with pytest.raises(ValueError, match=f"{changed_source} source changed"):
        reconciliation.reconcile_files(
            primary=primary, secondary=secondary, sport="NBA"
        )
