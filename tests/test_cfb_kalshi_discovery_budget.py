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
    assert coverage["discovery_requests"] <= 20
    assert coverage["reserved_book_requests"] >= 20
    assert len(rows) == 18
    assert all("_discovery_event" in row and "_discovery_series" in row for row in rows)


def test_cfb_discovery_budget_never_exceeds_public_client_ceiling():
    client = _PublicClient()
    _scope_kalshi(client)
    for _ in range(client.max_requests - client.request_count):
        client._request()
    assert client.request_count == client.max_requests
