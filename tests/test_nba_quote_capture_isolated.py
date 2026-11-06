"""NBA-only opt-in quote archive regression tests; no provider calls or BUYs."""
from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from parallax import league_quote_snapshots as archive
from parallax import nba_nhl
from parallax.models import Venue


def _fixture(sport="NBA"):
    return SimpleNamespace(
        sport=sport, game_id="bdl:123", season=2026,
        kickoff=datetime.now(UTC) + timedelta(hours=4),
        home_team="DEN", away_team="GSW",
    )


def _market():
    now = datetime.now(UTC).isoformat()
    return SimpleNamespace(
        venue=Venue.KALSHI,
        venue_market_id="KXNBAGAME-TEST-DEN",
        slug="KXNBAGAME-TEST-DEN",
        book_timestamp=now,
        status="OPEN",
        yes_bid=0.48, yes_ask=0.49, no_bid=0.50, no_ask=0.51,
        executable_depth={"YES": [(0.49, 100)], "NO": [(0.51, 100)]},
        mechanics=SimpleNamespace(
            fee_rate=0.07, fee_rounding="KALSHI_BALANCE",
            fee_status="VERIFIED_UPPER_BOUND",
            fee_valid_until=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            fee_source="tested", fee_observed_at=now,
            fee_buffer_per_contract=0,
        ),
    )


def test_archive_writes_sanitized_nba_quote_and_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "rules_digest", lambda market: "a" * 64)
    scenario = SimpleNamespace(
        available=True, reason="OK", contracts_or_shares=25,
        fees_estimate=0.5, total_cost=12.75,
        maximum_loss_including_fees=12.75,
    )
    monkeypatch.setattr(archive, "retail_example", lambda *a, **kw: scenario)
    store = archive.LeagueQuoteSnapshotStore(tmp_path / "quotes")
    market = _market()
    observed = datetime.now(UTC).isoformat()
    first = store.record(
        sport="NBA", fixture=_fixture(), market=market,
        observed_at=observed, yes_team="DEN",
    )
    assert first is not None
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert stat.S_IMODE(first.parent.stat().st_mode) == 0o700
    assert store.record(
        sport="NBA", fixture=_fixture(), market=market,
        observed_at=observed, yes_team="DEN",
    ) is None
    rows = [json.loads(line) for line in first.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["sport"] == "NBA"
    assert rows[0]["fee_valid_until"]
    assert rows[0]["executable_depth"]["YES"] == [[0.49, 100.0]]
    assert "credential" not in first.read_text().lower()


def test_nba_scan_records_without_granting_buy(monkeypatch):
    market = _market()
    counts = []
    class Recorder:
        def record(self, **kwargs):
            counts.append(kwargs)
            return None
    monkeypatch.setattr(nba_nhl, "normalize_kalshi", lambda *a, **kw: market)
    monkeypatch.setattr(nba_nhl, "_market_matches", lambda *a, **kw: True)
    monkeypatch.setattr(nba_nhl, "attach_fees", lambda m, *a, **kw: m)
    monkeypatch.setattr(nba_nhl, "_selected", lambda *a, **kw: "DEN")
    monkeypatch.setattr(nba_nhl, "predict_future", lambda *a, **kw: object())
    monkeypatch.setattr(nba_nhl, "shadow_evidence", lambda *a, **kw: None)
    raw = {"ticker": "KXNBAGAME-TEST-DEN"}
    result = nba_nhl.scan_league(
        "NBA", [_fixture()], [], [raw], [],
        kalshi_client=SimpleNamespace(book=lambda ticker: {"yes": []}),
        quote_snapshot_store=Recorder(),
    )
    assert len(counts) == 1
    assert counts[0]["sport"] == "NBA"
    assert result["quote_snapshots"]["deduplicated"] == 1
    assert result["alerts"] == 0
    assert result["rows"] == []


def test_nhl_does_not_write_nba_quotes(monkeypatch):
    result = nba_nhl.scan_league(
        "NHL", [], [], [], [],
        quote_snapshot_store=SimpleNamespace(record=lambda **kw: pytest.fail("NHL recording")),
    )
    assert result["quote_snapshots"]["enabled"] is False
    assert result["quote_snapshots"]["written"] == 0
