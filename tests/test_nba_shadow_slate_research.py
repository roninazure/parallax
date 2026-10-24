"""No live requests: bounded NBA shadow-scanner budget and input safety."""
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from scripts import nba_shadow_slate_research as runner

NOW = datetime(2026, 10, 23, 21, tzinfo=UTC)


def game():
    return {
        "id": 123, "season": 2026, "date": "2026-10-24",
        "datetime": "2026-10-24T22:00:00Z", "postseason": False,
        "postponed": False, "status_state": "scheduled", "season_type": "regular",
        "ist_stage": None,
        "home_team": {"abbreviation": "CHA", "city": "Charlotte"},
        "visitor_team": {"abbreviation": "MIA", "city": "Miami"},
    }


def test_read_secret_from_private_file_only(tmp_path):
    path = tmp_path / "key"
    path.write_text("sample-secret-for-test\n")
    path.chmod(0o600)
    assert runner.read_provider_key(path) == "sample-secret-for-test"
    path.chmod(0o644)
    with pytest.raises(runner.ScanSkipped):
        runner.read_provider_key(path)
    path.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(runner.ScanSkipped):
        runner.read_provider_key(alias)


def test_unavailable_key_stops_before_network(tmp_path):
    with pytest.raises(runner.ScanSkipped):
        runner.read_provider_key(tmp_path / "not-there")


def test_guard_debits_before_run_and_throttles_restarts(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "BUDGET_FILE", tmp_path / ".nba-shadow-budget.json")
    monkeypatch.setattr(runner, "LOCK_FILE", tmp_path / ".nba-shadow.lock")
    with runner.guarded_run(now=NOW):
        state = json.loads(runner.BUDGET_FILE.read_text())
        assert state["runs_today"] == 1
        assert state["max_gets_per_run"] == 8
    with pytest.raises(runner.ScanSkipped):
        with runner.guarded_run(now=NOW + timedelta(minutes=1)):
            pytest.fail("Restart guard failed")
    with runner.guarded_run(now=NOW + timedelta(hours=2)):
        pass
    assert json.loads(runner.BUDGET_FILE.read_text())["runs_today"] == 2


def test_guard_does_not_retry_bad_state(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "BUDGET_FILE", tmp_path / ".nba-shadow-budget.json")
    monkeypatch.setattr(runner, "LOCK_FILE", tmp_path / ".nba-shadow.lock")
    runner.BUDGET_FILE.write_text("{corrupted", encoding="utf-8")
    with pytest.raises(runner.ScanSkipped):
        with runner.guarded_run(now=NOW):
            pytest.fail("Unparseable budget must stop")


def test_provider_denial_persists_cooldown(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "BUDGET_FILE", tmp_path / ".nba-shadow-budget.json")
    monkeypatch.setattr(runner, "LOCK_FILE", tmp_path / ".nba-shadow.lock")
    with runner.guarded_run(now=NOW):
        runner.provider_denied(now=NOW)
    with pytest.raises(runner.ScanSkipped):
        with runner.guarded_run(now=NOW + timedelta(hours=2)):
            pytest.fail("Denied provider must remain cooled down")


def test_bdl_fixture_request_combines_three_dates_in_one_get(monkeypatch):
    called = []
    def fake_fetch(url, *, key=None):
        called.append((url, key))
        return {"data": [game()], "meta": {"next_cursor": None}}
    monkeypatch.setattr(runner, "fetch_json", fake_fetch)
    fixtures, count = runner.bdl_fixtures(key="test-key", now=NOW)
    assert count == 1
    assert len(called) == 1
    url, key = called[0]
    parsed = parse_qs(urlparse(url).query)
    assert url.startswith(runner.BDL_URL)
    assert key == "test-key"
    assert parsed["season_type"] == ["regular"]
    assert parsed["seasons[]"] == ["2026"]
    assert parsed["dates[]"] == ["2026-10-23", "2026-10-24", "2026-10-25"]
    assert set(fixtures) == {"KXNBAGAME-26OCT24MIACHA"}


def test_kalshi_events_honors_pages_and_rejects_truncated_listing(monkeypatch):
    seen = []
    def fake_fetch(url, *, key=None):
        assert key is None
        seen.append(url)
        if len(seen) == 1:
            return {"events": [{"series_ticker": "KXNBAGAME", "event_ticker": "KXNBAGAME-A"}],
                    "cursor": "another-page"}
        return {"events": [{"series_ticker": "KXNBAGAME", "event_ticker": "KXNBAGAME-B"}],
                "cursor": None}
    monkeypatch.setattr(runner, "fetch_json", fake_fetch)
    events, pages = runner.kalshi_events()
    assert (set(events), pages, len(seen)) == (
        {"KXNBAGAME-A", "KXNBAGAME-B"}, 2, 2,
    )
    assert "cursor=another-page" in seen[1]


def test_kalshi_page_cap_fails_closed(monkeypatch):
    calls = []
    def fake_fetch(url, *, key=None):
        calls.append(url)
        return {"events": [], "cursor": f"page{len(calls)}"}
    monkeypatch.setattr(runner, "fetch_json", fake_fetch)
    with pytest.raises(ValueError, match="pagination incomplete"):
        runner.kalshi_events()
    assert len(calls) == runner.MAX_EVENT_PAGES == 3


def test_run_once_never_authorizes_publication(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "RESEARCH_OUTPUT", tmp_path / "forward")
    monkeypatch.setattr(runner, "bdl_fixtures", lambda **kw: ({}, 0))
    monkeypatch.setattr(runner, "kalshi_events", lambda: ({}, 1))
    monkeypatch.setattr(runner, "load_history", lambda: [])
    report = runner.run_once(key="test-only", max_games=2)
    assert report["mode"] == "RESEARCH_ONLY"
    assert report["sport"] == "NBA"
    assert report["request_budget_max"] == 8
    assert not any(report[field] for field in (
        "buy_authorized", "orders_authorized", "publish_authorized",
        "alerts_authorized",
    ))
    assert report["screened"] == []
    assert report["rejected"] == []


@pytest.mark.parametrize("limit", [0, 3, -1, True])
def test_bounded_game_cap_rejects_invalid_counts(limit):
    with pytest.raises(ValueError):
        runner.run_once(key="test-only", max_games=limit)
