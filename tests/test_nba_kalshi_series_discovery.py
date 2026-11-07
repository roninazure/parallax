"""NBA-only Kalshi series metadata collection regression tests (offline)."""
from datetime import UTC, datetime

import pytest


def _run(monkeypatch, *, sport="NBA", series_mode="present"):
    import sys
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import _league_live_scan as scanner

    class Fixture:
        sport = None  # supplied after class creation to avoid class scope shadowing
        kickoff = datetime(2026, 11, 8, 0, 30, tzinfo=UTC)
        away_team = "BOS"
        home_team = "ATL"

    Fixture.sport = sport
    ticker = "KXNBAGAME" if sport == "NBA" else "KXNHLGAME"
    event_ticker = f"{ticker}-26NOV07BOSATL"

    class Kalshi:
        request_count = 0
        def events_page(self, *, series_ticker, limit, cursor, with_nested_markets):
            assert series_ticker == ticker
            self.request_count += 1
            return {"events": [{
                "event_ticker": event_ticker,
                "series_ticker": ticker,
                "markets": [{"ticker": event_ticker + "-BOS",
                             "event_ticker": event_ticker}],
            }], "cursor": ""}
        def series(self, series_ticker):
            assert sport == "NBA", "NHL must not gain a series request"
            self.request_count += 1
            if series_mode == "error":
                raise OSError("provider unavailable")
            if series_mode == "mismatched":
                return {"ticker": "KXOTHER", "fee_type": "quadratic", "fee_multiplier": 1}
            return {"ticker": ticker, "fee_type": "quadratic_with_maker_fees",
                    "fee_multiplier": 1, "unrelated_field": "discard"}
        def close(self):
            pass

    class PMUS:
        def markets_page(self, *, limit, offset, retry_transport_errors):
            return {"markets": []}
        def close(self):
            pass

    class Acquisition:
        def discover(self, _key, fetch):
            return fetch()

    kalshi = Kalshi()
    monkeypatch.setattr(scanner, "KalshiPublicClient", lambda: kalshi)
    monkeypatch.setattr(scanner, "PolymarketUSPublicClient", PMUS)
    rows, pmus_rows, health, _, _ = scanner._live_markets(
        sport, datetime(2026, 11, 7, 18, tzinfo=UTC), Acquisition(), [Fixture()],
    )
    assert pmus_rows == []
    assert len(rows) == 1
    return rows[0], health["KALSHI"]["coverage"], kalshi.request_count


def test_nba_series_fee_metadata_is_observed_not_certified(monkeypatch):
    row, coverage, requests = _run(monkeypatch)
    assert row["_discovery_series"] == {
        "ticker": "KXNBAGAME", "fee_type": "quadratic_with_maker_fees",
        "fee_multiplier": 1,
    }
    assert coverage["nba_series_fee_evidence"] == "METADATA_OBSERVED_NOT_SCHEDULE_VERIFIED"
    assert requests == 2


@pytest.mark.parametrize("mode,status", [
    ("error", "SERIES_METADATA_REQUEST_FAILED"),
    ("mismatched", "MISSING_OR_MISMATCHED_SERIES_METADATA"),
])
def test_nba_fee_series_failure_stays_unverified_without_crashing_scan(monkeypatch, mode, status):
    row, coverage, _ = _run(monkeypatch, series_mode=mode)
    assert row["_discovery_series"] == {"ticker": "KXNBAGAME"}
    assert coverage["nba_series_fee_evidence"] == status


def test_nhl_discovery_does_not_fetch_extra_fee_series(monkeypatch):
    row, coverage, requests = _run(monkeypatch, sport="NHL")
    assert row["_discovery_series"] == {"ticker": "KXNHLGAME"}
    assert coverage["nba_series_fee_evidence"] == "NOT_APPLICABLE"
    assert requests == 1
