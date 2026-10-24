"""Offline-only NBA prospective frozen forecast versus BDL final tests."""
from datetime import UTC, datetime, timedelta
import json
import math

import pytest

from parallax import nba_forward_reconciliation as rec
from scripts import nba_forward_reconcile as runner

CAPTURE = datetime(2026, 10, 24, 2, 31, 38, tzinfo=UTC)
START = datetime(2026, 10, 24, 22, tzinfo=UTC)
AFTER = START + timedelta(hours=6)
GID = "bdl:21717901"
EVENT = "KXNBAGAME-26OCT24MIACHA"
P = 0.663788


def receipt(folder, *, p=P, captured=CAPTURE, matchup="MIA@CHA", start=START,
            gid=GID, event=EVENT, name="nba-shadow-20261024T023138888900Z.json"):
    folder.mkdir(parents=True, exist_ok=True)
    result = {
        "mode": "RESEARCH_ONLY", "sport": "NBA",
        "model": "EXPERIMENTAL_NOT_CERTIFIED / raw Elo",
        "buy_authorized": False, "orders_authorized": False,
        "publish_authorized": False, "alerts_authorized": False,
        "captured_at_utc": captured.isoformat(),
        "screened": [{
            "game_id": gid, "matchup": matchup, "event_ticker": event,
            "start_utc": start.isoformat(),
            "raw_elo_home_probability": p,
            "positions": [
                {"ticker": f"{event}-MIA", "side": "YES", "action": "PASS"},
                {"ticker": f"{event}-MIA", "side": "NO", "action": "WATCH"},
                {"ticker": f"{event}-CHA", "side": "YES", "action": "WATCH"},
                {"ticker": f"{event}-CHA", "side": "NO", "action": "PASS"},
            ],
        }],
        "rejected": [],
    }
    path = folder / name
    path.write_text(json.dumps(result), encoding="utf-8")
    return path


def final_game(*, state="final", home_score=117, away_score=102):
    return {
        "id": 21717901, "season": 2026, "date": "2026-10-24",
        "datetime": START.isoformat(),
        "postseason": False, "postponed": False, "season_type": "regular",
        "status_state": state, "ist_stage": None,
        "home_team": {"abbreviation": "CHA"},
        "visitor_team": {"abbreviation": "MIA"},
        "home_team_score": home_score, "visitor_team_score": away_score,
    }


def saved_page(games=None):
    return {"data": [final_game()] if games is None else games,
            "meta": {"next_cursor": None}}


def test_earliest_snapshot_is_frozen(tmp_path):
    receipt(tmp_path)
    receipt(tmp_path, p=0.2, captured=CAPTURE + timedelta(hours=1),
            name="nba-shadow-20261024T033138888900Z.json")
    frozen = rec.archived_forecasts(tmp_path, now=AFTER)
    assert frozen[GID].home_probability == P
    assert frozen[GID].captured_utc == CAPTURE
    assert rec.pending_date(frozen, set(), now=START + timedelta(hours=1)) is None
    assert rec.pending_date(frozen, set(), now=AFTER) == "2026-10-24"
    assert rec.pending_date(frozen, {GID}, now=AFTER) is None


@pytest.mark.parametrize("change", [
    {"p": 1.0}, {"p": float("nan")},
    {"event": "KXNBAGAME-26OCT24CHAMIA"},
    {"matchup": "CHA@MIA"}, {"gid": "bdl:bad"},
    {"captured": START + timedelta(seconds=1)},
])
def test_reject_invalid_or_postgame_receipts(tmp_path, change):
    receipt(tmp_path, **change)
    with pytest.raises(rec.ReconciliationError):
        rec.archived_forecasts(tmp_path, now=AFTER)


def test_stale_or_malformed_archive_fails_closed(tmp_path):
    path = receipt(tmp_path)
    obj = json.loads(path.read_text())
    obj["screened"][0]["positions"][0]["action"] = "BUY"
    path.write_text(json.dumps(obj))
    with pytest.raises(rec.ReconciliationError):
        rec.archived_forecasts(tmp_path, now=AFTER)


def test_archive_game_identity_cannot_change_between_snapshots(tmp_path):
    receipt(tmp_path)
    receipt(tmp_path, matchup="CHA@MIA", event="KXNBAGAME-26OCT24CHAMIA",
            name="nba-shadow-20261024T033138888900Z.json")
    with pytest.raises(rec.ReconciliationError):
        rec.archived_forecasts(tmp_path, now=AFTER)


def test_exact_saved_final_scored_without_provider(tmp_path):
    receipt(tmp_path)
    frozen = rec.archived_forecasts(tmp_path, now=AFTER)
    rows, pending = rec.reconcile_day(
        frozen, saved_page(), requested_date="2026-10-24",
        reconciled_ids=set(), observed=AFTER,
        regular_query_verified=True,
    )
    assert pending == []
    assert len(rows) == 1
    row = rows[0]
    assert row["game_id"] == GID
    assert row["home_won"] is True
    assert row["winner"] == "CHA"
    assert row["brier"] == pytest.approx((P-1)**2)
    assert row["log_loss"] == pytest.approx(-math.log(P))
    assert all(row[k] is False for k in (
        "buy_authorized", "publication_authorized", "orders_authorized",
    ))
    assert rec.summary(rows)["n"] == 1


@pytest.mark.parametrize("state", ["scheduled", "live", "postponed"])
def test_nonfinal_never_counts_as_a_loss(tmp_path, state):
    receipt(tmp_path)
    frozen = rec.archived_forecasts(tmp_path, now=AFTER)
    rows, pending = rec.reconcile_day(
        frozen, saved_page([final_game(state=state)]),
        requested_date="2026-10-24", reconciled_ids=set(),
        observed=AFTER, regular_query_verified=True,
    )
    assert rows == []
    assert pending == [GID]


@pytest.mark.parametrize("change", [
    {"season": 2025}, {"postseason": True},
    {"date": "2026-10-25"}, {"home_team": {"abbreviation": "MIA"}},
    {"visitor_team": {"abbreviation": "CHA"}},
    {"datetime": "2026-10-24T23:00:00Z"},
    {"home_team_score": 102, "visitor_team_score": 102},
])
def test_mismatched_final_rejected(tmp_path, change):
    receipt(tmp_path)
    frozen = rec.archived_forecasts(tmp_path, now=AFTER)
    with pytest.raises(rec.ReconciliationError):
        rec.reconcile_day(
            frozen, saved_page([{**final_game(), **change}]),
            requested_date="2026-10-24", reconciled_ids=set(),
            observed=AFTER, regular_query_verified=True,
        )


def test_cursor_incomplete_fails_closed(tmp_path):
    receipt(tmp_path)
    frozen = rec.archived_forecasts(tmp_path, now=AFTER)
    data = saved_page()
    data["meta"]["next_cursor"] = 123
    with pytest.raises(rec.ReconciliationError):
        rec.reconcile_day(
            frozen, data, requested_date="2026-10-24",
            reconciled_ids=set(), observed=AFTER, regular_query_verified=True,
        )


def test_explicit_regular_filter_attestation_required(tmp_path):
    receipt(tmp_path)
    frozen = rec.archived_forecasts(tmp_path, now=AFTER)
    with pytest.raises(rec.ReconciliationError):
        rec.reconcile_day(
            frozen, saved_page(), requested_date="2026-10-24",
            reconciled_ids=set(), observed=AFTER, regular_query_verified=False,
        )


def configure_runner(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "ARCHIVE", tmp_path / "forward")
    monkeypatch.setattr(runner, "LEDGER", tmp_path / "forward/reconciled")
    monkeypatch.setattr(runner, "LOCK_FILE", tmp_path / ".nba-reconcile.lock")


def test_runner_offline_immutable_ledger_and_no_duplicate(tmp_path, monkeypatch):
    configure_runner(monkeypatch, tmp_path)
    receipt(runner.ARCHIVE)
    source = tmp_path / "verified-games.json"
    source.write_text(json.dumps(saved_page()))
    first = runner.run(games_json=source, regular_query_verified=True, now=AFTER)
    assert first["network_gets"] == 0
    assert first["new_finals"] == 1
    assert first["forward_metrics"]["n"] == 1
    ledger = runner.LEDGER / "nba-21717901.json"
    assert ledger.is_file()
    assert json.loads(ledger.read_text())["winner"] == "CHA"
    second = runner.run(games_json=source, regular_query_verified=True, now=AFTER)
    assert second["status"] == "NOTHING_MATURED"
    assert second["new_finals"] == 0
    assert list(runner.LEDGER.glob("nba-*.json")) == [ledger]


def test_corrupted_stored_metrics_refused(tmp_path, monkeypatch):
    configure_runner(monkeypatch, tmp_path)
    receipt(runner.ARCHIVE)
    source = tmp_path / "verified-games.json"
    source.write_text(json.dumps(saved_page()))
    runner.run(games_json=source, regular_query_verified=True, now=AFTER)
    ledger = runner.LEDGER / "nba-21717901.json"
    stored = json.loads(ledger.read_text())
    stored["brier"] = 0.0
    ledger.write_text(json.dumps(stored))
    with pytest.raises(rec.ReconciliationError):
        runner.run(games_json=source, regular_query_verified=True, now=AFTER)


def test_no_matured_forecast_is_zero_gets(tmp_path, monkeypatch):
    configure_runner(monkeypatch, tmp_path)
    receipt(runner.ARCHIVE)
    result = runner.run(
        games_json=tmp_path/"missing.json", regular_query_verified=True,
        now=START+timedelta(minutes=10),
    )
    assert result["status"] == "NOTHING_MATURED"
    assert result["network_gets"] == 0


def test_tied_stored_final_is_rejected(tmp_path, monkeypatch):
    configure_runner(monkeypatch, tmp_path)
    receipt(runner.ARCHIVE)
    source = tmp_path / "verified-games.json"
    source.write_text(json.dumps(saved_page()))
    runner.run(games_json=source, regular_query_verified=True, now=AFTER)
    ledger = runner.LEDGER / "nba-21717901.json"
    stored = json.loads(ledger.read_text())
    stored["home_score"] = stored["away_score"]
    ledger.write_text(json.dumps(stored))
    with pytest.raises(rec.ReconciliationError):
        runner.run(games_json=source, regular_query_verified=True, now=AFTER)
