"""Offline, mocked tests of the NBA one-GET-per-UTC-day final reader."""
from datetime import UTC, datetime, timedelta
import json
import os
import urllib.error
from urllib.parse import parse_qs, urlparse

import pytest

from parallax.nba_forward_reconciliation import ReconciliationError
from scripts import nba_forward_reconcile as runner


CAPTURE = datetime(2026, 10, 23, 23, tzinfo=UTC)
START = datetime(2026, 10, 24, 22, tzinfo=UTC)
AFTER = datetime(2026, 10, 27, 14, tzinfo=UTC)
GID = 21717901
EVENT = "KXNBAGAME-26OCT24MIACHA"


def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "ARCHIVE", tmp_path / "forward")
    monkeypatch.setattr(runner, "LEDGER", tmp_path / "forward" / "reconciled")
    monkeypatch.setattr(runner, "SOURCES", tmp_path / "forward" / "final-sources")
    monkeypatch.setattr(runner, "BUDGET_FILE", tmp_path / ".nba-reconcile-budget.json")
    monkeypatch.setattr(runner, "LOCK_FILE", tmp_path / ".nba-reconcile.lock")
    monkeypatch.setattr(runner, "read_provider_key", lambda: "test-key-not-real")


def receipt(path, *, gid=GID, start=START, matchup="MIA@CHA", event=EVENT):
    path.parent.mkdir(parents=True, exist_ok=True)
    positions = [
        {"ticker": event + "-" + team, "side": side, "action": "WATCH"}
        for team in matchup.split("@") for side in ("YES", "NO")
    ]
    obj = {
        "mode": "RESEARCH_ONLY", "sport": "NBA",
        "model": "EXPERIMENTAL_NOT_CERTIFIED / raw Elo",
        "buy_authorized": False, "orders_authorized": False,
        "publish_authorized": False, "alerts_authorized": False,
        "captured_at_utc": CAPTURE.isoformat(),
        "screened": [{
            "game_id": f"bdl:{gid}", "matchup": matchup,
            "event_ticker": event, "start_utc": start.isoformat(),
            "raw_elo_home_probability": 0.663788, "positions": positions,
        }],
    }
    path.write_text(json.dumps(obj), encoding="utf-8")


def response(*, gid=GID, start=START, date="2026-10-24"):
    return {"data": [{
        "id": gid, "season": 2026, "date": date,
        "datetime": start.isoformat(),
        "postseason": False, "postponed": False,
        "season_type": "regular", "ist_stage": None,
        "status_state": "final",
        "home_team": {"abbreviation": "CHA"},
        "visitor_team": {"abbreviation": "MIA"},
        "home_team_score": 118, "visitor_team_score": 100,
    }], "meta": {"next_cursor": None}}


def test_live_uses_one_get_records_private_source_and_immutable_final(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    receipt(runner.ARCHIVE / "nba-shadow-test.json")
    requests = []
    def get(*, date, key):
        requests.append((date, key))
        return response()
    monkeypatch.setattr(runner, "fetch_bdl_final_date", get)
    result = runner.run(live=True, now=AFTER)
    assert result["status"] == "RESEARCH_ONLY"
    assert (result["network_gets"], result["new_finals"]) == (1, 1)
    assert requests == [("2026-10-24", "test-key-not-real")]
    assert result["forward_metrics"]["n"] == 1
    assert result["buy_authorized"] is False
    assert result["publish_authorized"] is False
    assert result["orders_authorized"] is False
    source_file = runner.SOURCES / os.path.basename(result["source_receipt"])
    assert json.loads(source_file.read_text()) == response()
    assert source_file.stat().st_mode & 0o777 == 0o600
    assert (runner.LEDGER / f"nba-{GID}.json").is_file()
    assert json.loads(runner.BUDGET_FILE.read_text())["reads_today"] == 1
    again = runner.run(live=True, now=AFTER + timedelta(minutes=10))
    assert again["status"] == "NOTHING_MATURED"
    assert len(requests) == 1


def test_one_get_per_utc_day_even_if_result_still_pending(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    receipt(runner.ARCHIVE / "nba-shadow-test.json")
    calls = []
    def not_final(*, date, key):
        calls.append(date)
        return {"data": [], "meta": {"next_cursor": None}}
    monkeypatch.setattr(runner, "fetch_bdl_final_date", not_final)
    first = runner.run(live=True, now=AFTER)
    assert first["awaiting_final"] == [f"bdl:{GID}"]
    assert first["network_gets"] == 1
    with pytest.raises(ReconciliationError, match="already spent"):
        runner.run(live=True, now=AFTER + timedelta(hours=1))
    assert calls == ["2026-10-24"]


def test_429_sets_24h_cooldown_and_does_not_retry(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    receipt(runner.ARCHIVE / "nba-shadow-test.json")
    calls = []
    def denied(*, date, key):
        calls.append(date)
        raise urllib.error.HTTPError("https://api.balldontlie.io/v1/games", 429,
                                     "Too many requests", {}, None)
    monkeypatch.setattr(runner, "fetch_bdl_final_date", denied)
    with pytest.raises(urllib.error.HTTPError):
        runner.run(live=True, now=AFTER)
    state = json.loads(runner.BUDGET_FILE.read_text())
    assert state["utc_day"] == AFTER.date().isoformat()
    assert datetime.fromisoformat(state["cooldown_until_utc"]) > datetime.now(UTC)
    assert len(calls) == 1


def test_no_matured_game_uses_zero_requests_and_zero_budget(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    receipt(runner.ARCHIVE / "nba-shadow-test.json")
    monkeypatch.setattr(runner, "fetch_bdl_final_date", lambda **kw: pytest.fail("No provider calls"))
    result = runner.run(live=True, now=START - timedelta(hours=1))
    assert result["status"] == "NOTHING_MATURED"
    assert result["network_gets"] == 0
    assert not runner.BUDGET_FILE.exists()


def test_missing_credentials_do_not_spend_daily_budget(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    receipt(runner.ARCHIVE / "nba-shadow-test.json")
    def absent():
        raise ValueError("No credential")
    monkeypatch.setattr(runner, "read_provider_key", absent)
    with pytest.raises(ValueError, match="No credential"):
        runner.run(live=True, now=AFTER)
    assert not runner.BUDGET_FILE.exists()


def test_budget_malformed_fails_before_any_provider_call(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    receipt(runner.ARCHIVE / "nba-shadow-test.json")
    runner.BUDGET_FILE.write_text('{"utc_day":"nonsense","reads_today":1}')
    monkeypatch.setattr(runner, "fetch_bdl_final_date", lambda **kw: pytest.fail("No provider calls"))
    with pytest.raises(ReconciliationError, match="budget corrupted"):
        runner.run(live=True, now=AFTER)


def test_unresolved_dates_rotate_without_hammering_previous_date(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    receipt(runner.ARCHIVE / "nba-shadow-first.json")
    receipt(runner.ARCHIVE / "nba-shadow-second.json", gid=21717902,
            start=START + timedelta(days=1), matchup="BKN@CLE",
            event="KXNBAGAME-26OCT25BKNCLE")
    seen = []
    def unresolved(*, date, key):
        seen.append(date)
        return {"data": [], "meta": {"next_cursor": None}}
    monkeypatch.setattr(runner, "fetch_bdl_final_date", unresolved)
    first = runner.run(live=True, now=AFTER)
    second = runner.run(live=True, now=AFTER + timedelta(days=1))
    assert first["nba_date_et"] == "2026-10-24"
    assert second["nba_date_et"] == "2026-10-25"
    assert seen == ["2026-10-24", "2026-10-25"]
    assert json.loads(runner.BUDGET_FILE.read_text())["reads_today"] == 1


def test_bdl_get_url_is_exactly_one_date_and_auth_is_header_only(monkeypatch):
    requested = []
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(response()).encode()
    def fake_urlopen(request, *, timeout):
        requested.append((request, timeout))
        return FakeResponse()
    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)
    result = runner.fetch_bdl_final_date(date="2026-10-24", key="secret-test-not-real")
    assert result == response()
    assert len(requested) == 1
    request, timeout = requested[0]
    query = parse_qs(urlparse(request.full_url).query)
    assert query == {
        "dates[]": ["2026-10-24"], "seasons[]": ["2026"],
        "season_type": ["regular"], "per_page": ["100"],
    }
    assert "secret-test-not-real" not in request.full_url
    assert request.get_header("Authorization") == "secret-test-not-real"
    assert timeout == 15
