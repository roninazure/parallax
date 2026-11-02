"""Synthetic-only Polymarket US NBA exact-slug discovery tests.

No provider calls, no model certification, no production writes.
"""
from datetime import UTC, datetime, timedelta

from parallax.league_fixtures import ShadowFixture
from parallax.league_market_identity import expected_pmus_nba_slug
from scripts._league_live_scan import (
    PMUS_NBA_BATCH_SIZE,
    PMUS_NBA_MAX_FIXTURES,
    _discover_nba_pmus,
)

NOW = datetime(2026, 10, 19, 17, tzinfo=UTC)
TIP = datetime(2026, 10, 27, 2, tzinfo=UTC)
SLUG = "aec-nba-gs-den-2026-10-26"
DESCRIPTION = (
    "This market will settle to the winner of the GS Warriors vs DEN Nuggets "
    "professional basketball game originally scheduled for Oct 26, 2026 at "
    "10:00PM ET. Overtime is included if played. If the game is delayed, "
    "postponed, suspended, or otherwise rescheduled and is not rescheduled "
    "to start within two calendar days of the originally scheduled date "
    "and time, the market will settle to the last fair market price."
)


# Sanitized November 1 live US API settlement shape. Keep the alternate
# postponement payout clause explicit: this is not a generic text match.
CURRENT_DESCRIPTION = (
    "This market will settle to the winner of the GS Warriors vs DEN Nuggets "
    "professional basketball game scheduled for October 26, 2026 at 10:00PM ET "
    "(as of the listing of the event's first instrument). Overtime is included "
    "if played. If the game is delayed, postponed, suspended or otherwise "
    "rescheduled to a date and time not within two days of the originally "
    "scheduled date and time, the market will settle to the last fair market "
    "price. Outcome sourced from the relevant governing body."
)


def fixture(i=0):
    return ShadowFixture(
        "NBA", f"bdl:{i+1}", 2026, TIP + timedelta(days=i),
        "DEN", "GSW", "Denver", "Golden State",
    )


def market(**changes):
    raw = {
        "id": 1386496, "slug": SLUG, "status": "MARKET_STATUS_OPEN",
        "marketType": "moneyline", "gameStartTime": TIP.isoformat(),
        "description": DESCRIPTION, "marketSides": [
            {"long": True, "description": "Warriors",
             "team": {"abbreviation": "GS", "name": "Warriors",
                      "league": "NBA", "ordering": "away"}},
            {"long": False, "description": "Nuggets",
             "team": {"abbreviation": "DEN", "name": "Nuggets",
                      "league": "NBA", "ordering": "home"}},
        ],
    }
    raw.update(changes)
    return {
        "id": "1386496", "slug": SLUG, "event_id": "nba-gs-den",
        "question": "Who will win: GS Warriors vs DEN Nuggets?",
        "active": True, "closed": False, "accepting_orders": True, "raw": raw,
    }


class Acquisition:
    def __init__(self):
        self.keys = []

    def discover(self, key, request):
        self.keys.append(key)
        return request()


class Provider:
    def __init__(self, rows=None, *, error_batch=None):
        self.rows = rows or []
        self.calls = []
        self.error_batch = error_batch

    def markets_page(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == self.error_batch:
            raise RuntimeError("synthetic transport failure")
        return [row for row in self.rows if row["slug"] in kwargs["slugs"]]


def test_exact_slug_derivation_preserves_us_codes_and_et_day():
    assert expected_pmus_nba_slug(fixture()) == SLUG
    assert expected_pmus_nba_slug(ShadowFixture(
        "NBA", "nyk", 2026, TIP, "SAS", "NYK", "San Antonio", "New York",
    )) == "aec-nba-ny-sa-2026-10-26"


def test_exact_filtered_discovery_verifies_identity_without_books():
    acq, provider = Acquisition(), Provider([market()])
    rows, diag = _discover_nba_pmus(NOW, acq, provider, [fixture()])
    assert len(rows) == 1
    assert diag["coverage"]["state"] == "BOUNDED"
    assert diag["coverage"]["identity_verified"] == 1
    assert diag["fixture_diagnostics"][0]["discovery_state"] == "IDENTITY_VERIFIED"
    assert diag["fixture_diagnostics"][0]["book_state"] == "NOT_CHECKED"
    assert provider.calls == [{
        "limit": 1, "offset": 0, "slugs": [SLUG],
        "retry_transport_errors": False,
    }]
    assert len(acq.keys) == 1


def test_missing_filtered_result_is_not_proof_of_absent_exchange_market():
    rows, diag = _discover_nba_pmus(NOW, Acquisition(), Provider(), [fixture()])
    assert rows == []
    assert diag["fixture_diagnostics"][0]["discovery_state"] == "NOT_RETURNED_UNVERIFIED"
    assert diag["coverage"]["state"] == "BOUNDED"


def test_reject_wrong_settlement_and_start_time_without_book_requests():
    for bad in (
        market(marketType="spread"),
        market(sportsMarketTypeV2="SPORTS_MARKET_TYPE_SPREAD"),
        market(sportsMarketTypeV2="SPORTS_MARKET_TYPE_TOTAL"),
        market(sportsMarketTypeV2="SPORTS_MARKET_TYPE_UNSPECIFIED"),
        market(marketType="spread", sportsMarketTypeV2="SPORTS_MARKET_TYPE_MONEYLINE"),
        market(gameStartTime="2026-10-27T03:00:00+00:00"),
        market(description="winner determined by first quarter"),
    ):
        rows, diag = _discover_nba_pmus(
            NOW, Acquisition(), Provider([bad]), [fixture()],
        )
        assert rows == []
        assert diag["fixture_diagnostics"][0]["discovery_state"] == "IDENTITY_REJECTED"


def test_reject_malformed_market_shape_and_wrong_filtered_response():
    bad = market(marketSides=None)
    rows, diag = _discover_nba_pmus(NOW, Acquisition(), Provider([bad]), [fixture()])
    assert rows == []
    assert diag["fixture_diagnostics"][0]["discovery_state"] == "SHAPE_REJECTED"

    class WrongProvider(Provider):
        def markets_page(self, **kwargs):
            self.calls.append(kwargs)
            return [{**market(), "slug": "unrelated-market"}]

    rows, diag = _discover_nba_pmus(NOW, Acquisition(), WrongProvider(), [fixture()])
    assert rows == []
    assert diag["coverage"]["state"] == "PARTIAL"
    assert diag["fixture_diagnostics"][0]["discovery_state"] == "DISCOVERY_ERROR"


def test_two_request_budget_and_unqueried_fixture_diagnostics():
    fixtures = [fixture(i) for i in range(PMUS_NBA_MAX_FIXTURES + 2)]
    provider = Provider()
    rows, diag = _discover_nba_pmus(NOW, Acquisition(), provider, fixtures)
    assert rows == []
    assert len(provider.calls) == 2
    assert all(call["limit"] <= PMUS_NBA_BATCH_SIZE for call in provider.calls)
    assert diag["coverage"]["state"] == "PARTIAL"
    assert diag["coverage"]["queried_fixtures"] == PMUS_NBA_MAX_FIXTURES
    assert [row["discovery_state"] for row in diag["fixture_diagnostics"][-2:]] == [
        "NOT_QUERIED_BUDGET", "NOT_QUERIED_BUDGET",
    ]


def test_second_batch_failure_does_not_claim_missing_markets():
    fixtures = [fixture(i) for i in range(PMUS_NBA_BATCH_SIZE + 1)]
    provider = Provider(error_batch=2)
    rows, diag = _discover_nba_pmus(NOW, Acquisition(), provider, fixtures)
    assert rows == []
    assert len(provider.calls) == 2
    assert diag["coverage"]["state"] == "PARTIAL"
    assert diag["coverage"]["error_type"] == "RuntimeError"
    assert diag["fixture_diagnostics"][-1]["discovery_state"] == "DISCOVERY_ERROR"


def test_no_upcoming_fixtures_avoids_all_requests():
    provider = Provider()
    rows, diag = _discover_nba_pmus(
        NOW, Acquisition(), provider, [fixture(-30)],
    )
    assert rows == []
    assert provider.calls == []
    assert diag["coverage"]["fixture_candidates"] == 0


def test_book_outcomes_are_distinct_from_discovery_and_identity():
    from parallax.nba_nhl import scan_league

    class Book:
        def __init__(self, value=None, fail=False):
            self.value = value
            self.fail = fail

        def book(self, slug):
            assert slug == SLUG
            if self.fail:
                raise RuntimeError("synthetic book outage")
            return self.value

    complete = {
        f"{SLUG}::YES": {"best_ask": 0.51, "best_bid": 0.49},
        f"{SLUG}::NO": {"best_ask": 0.51, "best_bid": 0.49},
    }
    for client, expected in (
        (Book(complete), "TWO_SIDED_QUOTES_OBSERVED"),
        (Book({}), "BOOK_MISSING_OR_INCOMPLETE"),
        (Book(fail=True), "BOOK_REQUEST_ERROR"),
    ):
        result = scan_league(
            "NBA", [fixture()], [], [], [market()],
            pmus_client=client, now=NOW,
        )
        assert result["model_validation_state"] == "BLOCKED"
        assert result["orders"] == result["published"] == 0
        assert result["venues"]["PMUS"]["book_states"][SLUG] == expected


def test_nba_sdk_zero_retry_option_is_opt_in(monkeypatch):
    import polymarket_us
    from maker_spread_economics.polymarket_us import PolymarketUSPublicClient

    options = []

    class SDK:
        def __init__(self, **kwargs):
            options.append(kwargs)

    monkeypatch.setattr(polymarket_us, "PolymarketUS", SDK)
    PolymarketUSPublicClient(sdk_max_retries=0)
    PolymarketUSPublicClient()
    assert options[0]["max_retries"] == 0
    assert "max_retries" not in options[1]


def test_explicit_pmus_us_v2_moneyline_type_remains_supported():
    rows, diag = _discover_nba_pmus(
        NOW, Acquisition(),
        Provider([market(sportsMarketTypeV2="SPORTS_MARKET_TYPE_MONEYLINE")]),
        [fixture()],
    )
    assert len(rows) == 1
    assert diag["coverage"]["identity_verified"] == 1
    assert diag["fixture_diagnostics"][0]["book_state"] == "NOT_CHECKED"


def test_current_observed_us_rules_match_exact_full_game_contract():
    observed = market(description=CURRENT_DESCRIPTION,
                      sportsMarketTypeV2="SPORTS_MARKET_TYPE_MONEYLINE")
    # Current US sides may carry a city+nickname name while settlement text
    # uses the short outcome label. Team code and ordering still must match.
    observed["raw"]["marketSides"][0]["team"]["name"] = "Golden State Warriors"
    observed["raw"]["marketSides"][1]["team"]["name"] = "Denver Nuggets"
    rows, diag = _discover_nba_pmus(
        NOW, Acquisition(), Provider([observed]), [fixture()],
    )
    assert len(rows) == 1
    assert diag["coverage"]["identity_verified"] == 1


def test_current_us_rules_fail_closed_on_settlement_or_identity_drift():
    mutations = (
        CURRENT_DESCRIPTION.replace("Overtime is included", "Overtime is excluded"),
        CURRENT_DESCRIPTION.replace("last fair market price", "original market price"),
        CURRENT_DESCRIPTION.replace("October 26", "October 27"),
        CURRENT_DESCRIPTION.replace("10:00PM", "9:00PM"),
        CURRENT_DESCRIPTION.replace("GS Warriors", "LAL Lakers"),
        CURRENT_DESCRIPTION.replace("within two days", "within three days"),
        CURRENT_DESCRIPTION.replace("Outcome sourced from the relevant governing body.", ""),
    )
    for rules in mutations:
        rows, diag = _discover_nba_pmus(
            NOW, Acquisition(), Provider([market(description=rules)]), [fixture()],
        )
        assert rows == []
        assert diag["fixture_diagnostics"][0]["discovery_state"] == "IDENTITY_REJECTED"
