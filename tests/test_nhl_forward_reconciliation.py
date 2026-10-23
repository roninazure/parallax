"""Offline validation of frozen NHL predictions vs independent final scores."""
from datetime import UTC, datetime, timedelta
import json
import plistlib
import math
from pathlib import Path
import urllib.error

import pytest

from parallax import nhl_forward_reconciliation as rec
from scripts import nhl_forward_reconcile as runner


CAPTURE = datetime(2026, 10, 23, 18, 46, 55, tzinfo=UTC)
START = datetime(2026, 10, 24, 0, 0, tzinfo=UTC)
AFTER = START + timedelta(hours=6)
GAME_ID = "nhl:2026020166"
EVENT = "KXNHLGAME-26OCT23MTLCHI"


def snapshot(directory: Path, *, captured=CAPTURE, p=0.257922,
             game_id=GAME_ID, matchup="MTL@CHI", start=START,
             event=EVENT, name="nhl-shadow-20261023T184655000000Z.json"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    doc = {
        "mode": "RESEARCH_ONLY",
        "buy_authorized": False,
        "orders_authorized": False,
        "publish_authorized": False,
        "model": "EXPERIMENTAL_NOT_CERTIFIED / raw Elo (calibration not integrated)",
        "captured_at_utc": captured.isoformat(),
        "screened": [{
            "game_id": game_id,
            "matchup": matchup,
            "event_ticker": event,
            "start_utc": start.isoformat(),
            "raw_elo_home_probability": p,
            "positions": [{"action": "WATCH"}, {"action": "PASS"},
                          {"action": "WATCH"}, {"action": "PASS"}],
        }],
        "rejected": [],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def official(*, state="OFF", home=3, away=4):
    return {
        "id": 2026020166, "season": 20262027,
        "gameType": 2, "gameState": state,
        "gameDate": "2026-10-23", "startTimeUTC": START.isoformat(),
        "awayTeam": {"abbrev": "MTL", "score": away},
        "homeTeam": {"abbrev": "CHI", "score": home},
    }


def scoreboard(games=None):
    return {"currentDate": "2026-10-23", "games": games if games is not None else [official()]}


def test_frozen_first_observation_no_retroactive_selection(tmp_path):
    snapshot(tmp_path)
    snapshot(tmp_path, captured=CAPTURE + timedelta(minutes=20),
             p=0.9, name="nhl-shadow-20261023T190655000000Z.json")
    actual = rec.archived_forecasts(tmp_path, now=AFTER)
    f = actual[GAME_ID]
    assert f.home_probability == 0.257922
    assert f.forecast_captured_utc == CAPTURE
    assert rec.scoreboard_date(f) == "2026-10-23"  # midnight UTC != local calendar day


@pytest.mark.parametrize("new_value", [
    {"matchup": "CHI@MTL"}, {"game_id": "nhl:bad"},
    {"p": 1.0}, {"p": float("nan")},
    {"captured": START + timedelta(seconds=1)},
])
def test_invalid_pregame_archive_rejected(tmp_path, new_value):
    snapshot(tmp_path, **new_value)
    with pytest.raises(rec.ReconciliationError):
        rec.archived_forecasts(tmp_path, now=AFTER)


def test_identity_conflict_across_two_snapshots_fails_closed(tmp_path):
    snapshot(tmp_path)
    snapshot(tmp_path, event="KXNHLGAME-26OCT23CHIMTL",
             name="nhl-shadow-20261023T192655000000Z.json")
    with pytest.raises(rec.ReconciliationError, match="Conflicting"):
        rec.archived_forecasts(tmp_path, now=AFTER)


def test_earliest_matured_date_and_no_lookahead(tmp_path):
    snapshot(tmp_path)
    f = rec.archived_forecasts(tmp_path, now=AFTER)
    assert rec.pending_date(f, set(), now=START + timedelta(hours=2)) is None
    assert rec.pending_date(f, set(), now=AFTER) == "2026-10-23"
    assert rec.pending_date(f, {GAME_ID}, now=AFTER) is None


def test_official_full_game_winner_and_prospective_metrics(tmp_path):
    snapshot(tmp_path)
    f = rec.archived_forecasts(tmp_path, now=AFTER)
    rows, pending = rec.reconcile_day(
        f, scoreboard(), requested_date="2026-10-23",
        reconciled_ids=set(), captured_at=AFTER,
    )
    assert pending == []
    assert len(rows) == 1
    row = rows[0]
    assert row["winner"] == "MTL"
    assert row["home_won"] is False
    assert row["home_score"] == 3 and row["away_score"] == 4
    assert row["home_probability"] == 0.257922
    assert row["forecast_captured_utc"] == CAPTURE.isoformat()
    assert row["brier"] == pytest.approx(0.257922 ** 2)
    assert row["log_loss"] == pytest.approx(-math.log(1 - 0.257922))
    assert row["publication_authorized"] is False
    assert row["buy_authorized"] is False
    assert rec.summary(rows)["n"] == 1


@pytest.mark.parametrize("change", [
    {"id": 2222222222}, {"season": 20252026}, {"gameType": 3},
    {"gameDate": "2026-10-24"}, {"startTimeUTC": "2026-10-24T01:00:00Z"},
    {"homeTeam": {"abbrev": "TOR", "score": 3}},
    {"awayTeam": {"abbrev": "ANA", "score": 4}},
    {"homeTeam": {"abbrev": "CHI", "score": 4}},  # 4-4 final invalid
])
def test_contradictory_finals_fail_closed(tmp_path, change):
    snapshot(tmp_path)
    f = rec.archived_forecasts(tmp_path, now=AFTER)[GAME_ID]
    with pytest.raises(rec.ReconciliationError):
        rec.official_final({**official(), **change}, f, requested_date="2026-10-23")


@pytest.mark.parametrize("state", ["FUT", "LIVE", "CRIT", "PRE"])
def test_nonfinal_not_a_loss(tmp_path, state):
    snapshot(tmp_path)
    forecasts = rec.archived_forecasts(tmp_path, now=AFTER)
    rows, pending = rec.reconcile_day(
        forecasts, scoreboard([{**official(), "gameState": state}]),
        requested_date="2026-10-23", reconciled_ids=set(), captured_at=AFTER,
    )
    assert not rows
    assert pending == [GAME_ID]


def test_wrong_scoreboard_date_fails_closed(tmp_path):
    snapshot(tmp_path)
    forecasts = rec.archived_forecasts(tmp_path, now=AFTER)
    with pytest.raises(rec.ReconciliationError, match="date mismatch"):
        rec.reconcile_day(forecasts, scoreboard(), requested_date="2026-10-24",
                          reconciled_ids=set(), captured_at=AFTER)


def config(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "ARCHIVE", tmp_path / "forward")
    monkeypatch.setattr(runner, "LEDGER", tmp_path / "forward/reconciled")
    monkeypatch.setattr(runner, "BUDGET_FILE", tmp_path / ".nhl-reconcile-budget.json")
    monkeypatch.setattr(runner, "LOCK_FILE", tmp_path / ".nhl-reconcile.lock")


def test_offline_reconciliation_creates_one_immutable_ledger(tmp_path, monkeypatch):
    config(monkeypatch, tmp_path)
    snapshot(runner.ARCHIVE)
    payload = tmp_path / "scoreboard.json"
    payload.write_text(json.dumps(scoreboard()), encoding="utf-8")
    first = runner.run(scoreboard_json=payload, now=AFTER)
    assert first["new_finals"] == 1
    assert first["scoreboard_gets"] == 0
    ledger = runner.LEDGER / "nhl-2026020166.json"
    assert json.loads(ledger.read_text())["winner"] == "MTL"
    second = runner.run(scoreboard_json=payload, now=AFTER)
    assert second["status"] == "NOTHING_MATURED"
    assert second["scoreboard_gets"] == 0
    assert len(list(runner.LEDGER.glob("nhl-*.json"))) == 1


def test_live_budget_one_scoreboard_daily_no_extra_provider_calls(
    tmp_path, monkeypatch,
):
    config(monkeypatch, tmp_path)
    snapshot(runner.ARCHIVE)
    seen = []
    def fetch(url):
        seen.append(url)
        return scoreboard([official(state="LIVE")])
    monkeypatch.setattr(runner, "fetch_json", fetch)
    result = runner.run(live=True, now=AFTER)
    assert result["scoreboard_gets"] == 1
    assert result["new_finals"] == 0
    assert result["awaiting_final"] == [GAME_ID]
    assert seen == ["https://api-web.nhle.com/v1/score/2026-10-23"]
    with pytest.raises(rec.ReconciliationError, match="already spent"):
        runner.run(live=True, now=AFTER + timedelta(minutes=10))
    assert len(seen) == 1


def test_live_denial_sets_cooldown(tmp_path, monkeypatch):
    config(monkeypatch, tmp_path)
    snapshot(runner.ARCHIVE)
    def denied(url):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(runner, "fetch_json", denied)
    with pytest.raises(urllib.error.HTTPError):
        runner.run(live=True, now=AFTER)
    state = json.loads(runner.BUDGET_FILE.read_text())
    assert datetime.fromisoformat(state["cooldown_until_utc"]) > datetime.now(UTC)


def test_no_matured_games_costs_no_provider_calls(tmp_path, monkeypatch):
    config(monkeypatch, tmp_path)
    snapshot(runner.ARCHIVE)
    monkeypatch.setattr(
        runner, "fetch_json",
        lambda url: pytest.fail("not allowed before final maturity"),
    )
    result = runner.run(live=True, now=START + timedelta(hours=1))
    assert result["scoreboard_gets"] == 0
    assert not runner.BUDGET_FILE.exists()



def test_market_identity_mismatch_rejected(tmp_path):
    snapshot(tmp_path, event="KXNHLGAME-26OCT23TORCGY")
    with pytest.raises(rec.ReconciliationError, match="Market ticker contradicts"):
        rec.archived_forecasts(tmp_path, now=AFTER)


def test_corrupted_ledger_stops_reconciliation(tmp_path, monkeypatch):
    config(monkeypatch, tmp_path)
    snapshot(runner.ARCHIVE)
    payload = tmp_path / "scoreboard.json"
    payload.write_text(json.dumps(scoreboard()), encoding="utf-8")
    runner.run(scoreboard_json=payload, now=AFTER)
    path = runner.LEDGER / "nhl-2026020166.json"
    broken = json.loads(path.read_text())
    broken["winner"] = "CHI"
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(rec.ReconciliationError, match="Corrupt"):
        runner.run(scoreboard_json=payload, now=AFTER)



def test_separate_reconciliation_daemon_template_is_non_root_daily():
    path = Path(__file__).resolve().parents[1] / (
        "ops/launchd/com.swarmaxis.parallax-nhl-forward-reconcile.plist"
    )
    with path.open("rb") as stream:
        p = plistlib.load(stream)
    assert p["Label"] == "com.swarmaxis.parallax-nhl-forward-reconcile"
    assert p["UserName"] == "scottsteele"
    assert p["StartInterval"] == 86400
    assert p["KeepAlive"] is False
    assert p["ProgramArguments"] == [
        "/Users/scottsteele/parallax-research/venv312/bin/python",
        "-m", "scripts.nhl_forward_reconcile", "--live",
    ]
    assert p["WorkingDirectory"].endswith("/parallax-research/parallax-shadow")
    assert "parallax-sports" not in p["Label"]
