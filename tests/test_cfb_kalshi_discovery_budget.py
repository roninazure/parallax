"""Regression tests for the bounded CFB Kalshi discovery/acquisition split."""
from scripts.cfb_live_scan import _scope_kalshi


class _PublicClient:
    max_requests = 40

    def __init__(self):
        self.request_count = 0

    def _request(self):
        if self.request_count >= self.max_requests:
            raise AssertionError("Kalshi public safety ceiling exceeded")
        self.request_count += 1

    def series_page(self, *, limit, cursor=""):
        self._request()
        return {"series": [{"ticker": "KXNCAAFGAME"}], "cursor": ""}

    def events_page(self, *, series_ticker, limit, cursor=""):
        self._request()
        return {
            "events": [{"ticker": f"KXNCAAFGAME-TEST-{i}"} for i in range(50)],
            "cursor": "",
        }

    def event_markets_page(self, *, event_ticker, limit, cursor=""):
        self._request()
        return {
            "markets": [{"ticker": event_ticker + "-HOME"}],
            "cursor": "",
        }


def test_cfb_discovery_reserves_at_least_half_public_request_budget():
    client = _PublicClient()
    rows, coverage = _scope_kalshi(client)
    assert rows
    assert coverage["state"] == "PARTIAL"
    assert coverage["discovery_requests"]  <= 8
    assert coverage["reserved_book_requests"]  >= 32
    assert len(rows) == 6
    assert all("_discovery_event" in row and "_discovery_series" in row for row in rows)


def test_cfb_discovery_budget_never_exceeds_public_client_ceiling():
    client = _PublicClient()
    _scope_kalshi(client)
    for _ in range(client.max_requests - client.request_count):
        client._request()
    assert client.request_count == client.max_requests

def test_pmus_discovery_queries_exact_upcoming_winner_slug(monkeypatch):
    from datetime import UTC, datetime
    from parallax.cfb import CFBGame
    from scripts import cfb_live_scan

    monkeypatch.setattr(
        cfb_live_scan, "utcnow",
        lambda: datetime(2026, 11, 22, 18, tzinfo=UTC),
    )
    game = CFBGame(
        "scheduled", 2026, "regular", "2026-11-27T00:30:00+00:00",
        "South Carolina", "Georgia", "1", "2", "fbs", "fbs",
        False, None, None, False,
    )

    class PMUS:
        def __init__(self):
            self.requests = []

        def markets_page(self, **kwargs):
            self.requests.append(kwargs)
            return [{
                "slug": kwargs["slugs"][0], "active": True, "closed": False,
                "raw": {"sport": "CFB"},
            }]

    client = PMUS()
    rows, coverage = cfb_live_scan._scope_pmus(client, [game])
    expected = "aec-cfb-ga-sc-2026-11-26"
    assert client.requests == [{
        "limit": 1, "offset": 0, "slugs": [expected],
        "retry_transport_errors": False,
    }]
    assert [row["slug"] for row in rows] == [expected]
    assert coverage["state"] == "TARGETED"


def test_pmus_unknown_team_fails_closed_before_provider_call(monkeypatch):
    from datetime import UTC, datetime
    from parallax.cfb import CFBGame
    from scripts import cfb_live_scan

    monkeypatch.setattr(
        cfb_live_scan, "utcnow",
        lambda: datetime(2026, 11, 22, 18, tzinfo=UTC),
    )
    unknown = CFBGame(
        "unknown", 2026, "regular", "2026-11-27T00:30:00+00:00",
        "Fictional School X", "Georgia", "1", "2", "fbs", "fbs",
        False, None, None, False,
    )

    class PMUS:
        def markets_page(self, **kwargs):
            raise AssertionError("no unknown-team venue query permitted")

    rows, coverage = cfb_live_scan._scope_pmus(PMUS(), [unknown])
    assert rows == []
    assert coverage["unknown_team_games"] == 1
    assert coverage["request_count"] == 0
    assert coverage["state"] == "PARTIAL"


def test_pmus_rejects_unexpected_provider_market(monkeypatch):
    from datetime import UTC, datetime
    from parallax.cfb import CFBGame
    from scripts import cfb_live_scan
    import pytest

    monkeypatch.setattr(
        cfb_live_scan, "utcnow",
        lambda: datetime(2026, 11, 22, 18, tzinfo=UTC),
    )
    game = CFBGame(
        "scheduled", 2026, "regular", "2026-11-27T00:30:00+00:00",
        "South Carolina", "Georgia", "1", "2", "fbs", "fbs",
        False, None, None, False,
    )

    class PMUS:
        def markets_page(self, **kwargs):
            return [{"slug": "aec-cfb-unrequested-other-2026-11-26"}]

    with pytest.raises(ValueError, match="escaped requested slug scope"):
        cfb_live_scan._scope_pmus(PMUS(), [game])


def test_kalshi_direct_series_fallback_when_first_series_page_is_empty():
    class Client(_PublicClient):
        def series_page(self, *, limit, cursor=""):
            self._request()
            return {"series": [], "cursor": ""}

        def events_page(self, *, series_ticker, limit, cursor=""):
            self._request()
            return {
                "events": [{"ticker": series_ticker + "-26NOV27-EXAMPLE"}],
                "cursor": "",
            }

    client = Client()
    rows, coverage = _scope_kalshi(client)
    assert rows
    assert coverage["direct_series_fallback"] is True
    assert coverage["state"] == "PARTIAL"
    assert coverage["discovery_requests"] <= 8
    assert coverage["reserved_book_requests"] >= 32
    assert {row["_discovery_series"]["ticker"] for row in rows} == {
        "KXNCAAFGAME", "KXCFBGAME",
    }
