"""Synthetic-only BALLDONTLIE Free-tier quota tests. No network traffic."""
from datetime import UTC, datetime
import os
import sqlite3

import pytest

from parallax.bdl_request_budget import (
    RequestBudgetExceeded, reserve_bdl_request,
)
from parallax import nba_nhl


def test_shared_rolling_window_rejects_fifth_request(tmp_path):
    state = tmp_path / "private"
    state.mkdir(mode=0o700)
    path = state / "budget.sqlite3"
    for i in range(4):
        reserve_bdl_request(path, now=100.0 + i)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RequestBudgetExceeded, match="budget exhausted"):
        reserve_bdl_request(path, now=104.0)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 4
    reserve_bdl_request(path, now=160.0)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 4


def test_clock_rollback_or_symlink_fails_closed(tmp_path):
    path = tmp_path / "budget.sqlite3"
    reserve_bdl_request(path, now=100.0)
    with pytest.raises(RequestBudgetExceeded, match="clock rollback"):
        reserve_bdl_request(path, now=99.0)
    other = tmp_path / "symlink.sqlite3"
    other.symlink_to(path)
    with pytest.raises(RequestBudgetExceeded, match="symlink"):
        reserve_bdl_request(other, now=101.0)


def test_untrusted_directory_or_ledger_permissions_rejected(tmp_path):
    path = tmp_path / "budget.sqlite3"
    reserve_bdl_request(path, now=100.0)
    os.chmod(path, 0o644)
    with pytest.raises(RequestBudgetExceeded, match="mode unsafe"):
        reserve_bdl_request(path, now=101.0)
    os.chmod(path, 0o600)
    os.chmod(tmp_path, 0o755)
    with pytest.raises(RequestBudgetExceeded, match="directory ownership/mode unsafe"):
        reserve_bdl_request(path, now=102.0)


def test_injected_nba_fixture_transport_never_reserves_provider_quota(monkeypatch):
    def unexpected_budget():
        raise AssertionError("synthetic fixture transport spent provider quota")
    monkeypatch.setattr(nba_nhl, "reserve_bdl_request", unexpected_budget)
    now = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
    payload = {"data": [], "meta": {"next_cursor": None}}
    assert nba_nhl.fetch_nba_fixtures(now=now, transport=lambda _: payload) == []


def test_live_nba_budget_rejection_precedes_network(monkeypatch):
    monkeypatch.setattr(nba_nhl, "_balldontlie_api_key", lambda: "synthetic-secret")
    def deny():
        raise RequestBudgetExceeded("synthetic quota exhausted")
    def illegal_network(*args, **kwargs):
        raise AssertionError("network access attempted on quota rejection")
    monkeypatch.setattr(nba_nhl, "reserve_bdl_request", deny)
    monkeypatch.setattr(nba_nhl, "urlopen", illegal_network)
    with pytest.raises(RequestBudgetExceeded):
        nba_nhl.fetch_nba_fixtures(
            now=datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
        )
