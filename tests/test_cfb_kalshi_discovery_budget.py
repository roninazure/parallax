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

    def series(self, ticker):
        self._request()
        return {"ticker": ticker, "fee_type": "quadratic", "fee_multiplier": 1}

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
    assert len(rows) == 5
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

def test_cfb_pmus_fee_worst_tier_is_reviewed_and_expires_closed():
    from dataclasses import dataclass, field
    from datetime import UTC, datetime
    from scripts.cfb_live_scan import _attach_cfb_pmus_fees

    @dataclass
    class Mechanics:
        fee_rate: float | None = None
        fee_source: str = ""
        fee_rounding: str = ""
        fee_status: str = "UNVERIFIED"
        fee_observed_at: str = ""
        fee_valid_until: str | None = None
        fee_buffer_per_contract: int = 0

    @dataclass
    class Market:
        mechanics: Mechanics = field(default_factory=Mechanics)
        original_metadata: dict = field(default_factory=dict)

    market = Market()
    now = datetime(2026, 11, 22, 20, tzinfo=UTC)
    reviewed = _attach_cfb_pmus_fees(market, now)
    assert reviewed.mechanics.fee_rate == 0.095
    assert reviewed.mechanics.fee_status == "VERIFIED_UPPER_BOUND"
    assert reviewed.mechanics.fee_valid_until == "2026-11-29T00:00:00+00:00"
    assert "rebates" in reviewed.original_metadata["fee_provenance"]["reason"]
    assert market.mechanics.fee_rate is None


def test_cfb_kalshi_review_is_scoped_and_expires_closed(monkeypatch):
    from dataclasses import dataclass, field
    from datetime import UTC, datetime
    from scripts import cfb_live_scan

    @dataclass
    class Mechanics:
        fee_rate: float | None = None
        fee_source: str = ""
        fee_rounding: str = ""
        fee_status: str = "UNVERIFIED"
        fee_observed_at: str = ""
        fee_valid_until: str | None = None
        fee_buffer_per_contract: int = 0
        fee_balance_precision: float = 0.01

    @dataclass
    class Market:
        event: str = "KXNCAAFGAME-26NOV28-TEST"
        venue_market_id: str = "KXNCAAFGAME-26NOV28-TEST-GA"
        data_timestamp: str = "2026-11-25T00:00:00+00:00"
        mechanics: Mechanics = field(default_factory=Mechanics)
        original_metadata: dict = field(default_factory=lambda: {"market": {
            "ticker": "KXNCAAFGAME-26NOV28-TEST-GA",
            "event_ticker": "KXNCAAFGAME-26NOV28-TEST",
        }})

    # Keep this regression independent of expired NFL/global fee review.
    monkeypatch.setattr(cfb_live_scan, "attach_fees", lambda market, now, **kwargs: market)
    market = Market()
    event = {"ticker": market.event, "series_ticker": "KXNCAAFGAME"}
    series = {"ticker": "KXNCAAFGAME", "fee_type": "quadratic", "fee_multiplier": 1}
    now = datetime(2026, 11, 25, tzinfo=UTC)
    approved = cfb_live_scan._attach_cfb_kalshi_fees(market, now, event=event, series=series)
    assert approved.mechanics.fee_rate == 0.07
    assert approved.mechanics.fee_status == "VERIFIED_UPPER_BOUND"
    assert approved.mechanics.fee_valid_until == "2026-11-25T00:01:00+00:00"
    assert market.mechanics.fee_status == "UNVERIFIED"
    unsupported = cfb_live_scan._attach_cfb_kalshi_fees(
        market, now, event=event, series={**series, "fee_type": "flat"},
    )
    assert unsupported.mechanics.fee_status == "UNVERIFIED"
    unknown = cfb_live_scan._attach_cfb_kalshi_fees(
        market, now, event=event, series={"ticker": "KXNCAAFGAME"},
    )
    assert unknown.mechanics.fee_status == "UNVERIFIED"
    expired = cfb_live_scan._attach_cfb_kalshi_fees(
        market, datetime(2026, 12, 1, tzinfo=UTC), event=event, series=series,
    )
    assert expired.mechanics.fee_status == "UNVERIFIED"


def test_cfb_pmus_rate_limit_does_not_abort_kalshi_discovery(monkeypatch):
    from maker_spread_economics.polymarket_us import PolymarketUSRateLimit
    from scripts import cfb_live_scan

    class NoNetwork:
        max_requests = 40
        request_count = 0
        def __init__(self, *args, **kwargs):
            pass
        def close(self):
            pass

    monkeypatch.setattr(cfb_live_scan, "TrackRecord", lambda *args: object())
    monkeypatch.setattr(cfb_live_scan, "fetch_games", lambda *args: [])
    monkeypatch.setattr(cfb_live_scan, "PolymarketUSPublicClient", NoNetwork)
    monkeypatch.setattr(cfb_live_scan, "KalshiPublicClient", NoNetwork)
    monkeypatch.setattr(cfb_live_scan, "_scope_pmus", lambda *args: (_ for _ in ()).throw(
        PolymarketUSRateLimit("simulated provider 429")
    ))
    kalshi_called = []
    def kalshi_ok(*args):
        kalshi_called.append(True)
        return [], {"state": "PARTIAL", "reason": "simulated"}
    monkeypatch.setattr(cfb_live_scan, "_scope_kalshi", kalshi_ok)

    result = cfb_live_scan._scan(alert_dispatcher=object())
    assert kalshi_called == [True]
    assert result["venues"]["PMUS"]["coverage"]["state"] == "UNAVAILABLE"
    assert result["venues"]["PMUS"]["coverage"]["error_type"] == "PolymarketUSRateLimit"
    assert result["venues"]["PMUS"]["discovered"] == 0
    assert result["venues"]["KALSHI"]["coverage"]["state"] == "PARTIAL"
    assert result["alerts"] == 0
    assert result["rows"] == []
