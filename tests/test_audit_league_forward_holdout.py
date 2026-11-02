"""Synthetic offline-only tests for prospective NBA/NHL evidence screening."""
from datetime import date, timedelta
import csv
import pytest
from scripts.audit_league_forward_holdout import audit

COLUMNS = ("sport", "game_id", "season", "game_date",
           "home_team", "away_team", "winner_team")


def write(path, sport, dates, prefix, *, winner="H"):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for i, day in enumerate(dates):
            writer.writerow({
                "sport": sport, "game_id": f"{prefix}-{i}", "season": "2026",
                "game_date": day.isoformat(), "home_team": "H", "away_team": "A",
                "winner_team": winner,
            })


@pytest.mark.parametrize("sport", ["NBA", "NHL"])
def test_forward_reconciliation_remains_noncertifying(tmp_path, sport):
    train, forward, independent = [tmp_path / x for x in ("train.csv", "future.csv", "other.csv")]
    write(train, sport, [date(2026, 10, 1)], "old")
    write(forward, sport, [date(2026, 10, 2)], "primary")
    write(independent, sport, [date(2026, 10, 2)], "secondary")
    result = audit(sport=sport, historical=train, forward=forward,
                   independent=independent, as_of=date(2026, 10, 30))
    assert result["forward_games"] == 1
    assert result["fixture_consistency_passed"]
    assert not result["provider_independence_verified"]
    assert not result["source_authorization_verified"]
    assert not result["model_calibration_verified"]
    assert not result["production_buy_authorized"]


@pytest.mark.parametrize("condition", ["old", "future", "winner", "duplicate", "identical"])
def test_invalid_forward_evidence_fails_closed(tmp_path, condition):
    train, forward, independent = [tmp_path / x for x in ("train.csv", "future.csv", "other.csv")]
    historical_day = date(2026, 10, 1)
    future_day = historical_day if condition == "old" else (
        date(2026, 11, 1) if condition == "future" else date(2026, 10, 2)
    )
    write(train, "NBA", [historical_day], "old")
    write(forward, "NBA", [future_day], "primary")
    write(independent, "NBA", [future_day], "secondary",
          winner="A" if condition == "winner" else "H")
    if condition == "duplicate":
        write(independent, "NBA", [future_day, future_day], "secondary")
    if condition == "identical":
        independent.write_bytes(forward.read_bytes())
    with pytest.raises(ValueError):
        audit(sport="NBA", historical=train, forward=forward,
              independent=independent, as_of=date(2026, 10, 30))


def test_modified_file_during_parse_is_rejected(tmp_path, monkeypatch):
    from scripts import audit_league_forward_holdout as module
    train, forward, independent = [tmp_path / x for x in ("train.csv", "future.csv", "other.csv")]
    write(train, "NBA", [date(2026, 10, 1)], "old")
    write(forward, "NBA", [date(2026, 10, 2)], "primary")
    write(independent, "NBA", [date(2026, 10, 2)], "secondary")
    original = module.load_final_games_csv

    def mutated(path):
        value = original(path)
        if path == forward:
            with path.open("a", encoding="utf-8") as f:
                f.write("\n")
        return value

    monkeypatch.setattr(module, "load_final_games_csv", mutated)
    with pytest.raises(ValueError, match="modified during parsing"):
        audit(sport="NBA", historical=train, forward=forward,
              independent=independent, as_of=date(2026, 10, 30))
