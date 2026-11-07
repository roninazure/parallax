"""NBA-only Polymarket US fee change: protect legacy NFL behavior."""
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from parallax.fees import attach_fees
from parallax.models import Mechanics, Venue


@dataclass(frozen=True)
class SampleMarket:
    venue: Venue = Venue.POLYMARKET
    category: str = "Sports"
    title: str = "NBA full game winner"
    description: str = "Regular-season outright"
    event_title: str = "NBA"
    mechanics: Mechanics = field(default_factory=Mechanics)
    original_metadata: dict = field(default_factory=dict)


@pytest.mark.parametrize(
    "at,sport,expected",
    [
        ("2026-11-06T17:59:59+00:00", "NBA", 0.0695),
        ("2026-11-06T18:00:00+00:00", "NBA", 0.095),
        ("2026-11-07T15:00:00+00:00", "NBA", 0.095),
        ("2026-11-07T15:00:00+00:00", "NFL", 0.0695),
        ("2026-11-07T15:00:00+00:00", "NHL", 0.0695),
        ("2026-11-07T15:00:00+00:00", None, 0.0695),
    ],
)
def test_only_nba_uses_post_cutover_maximum_standard_taker_rate(at, sport, expected):
    now = datetime.fromisoformat(at)
    market = attach_fees(SampleMarket(), now, sport=sport)
    assert market.mechanics.fee_rate == expected
    assert market.mechanics.fee_status == "VERIFIED_UPPER_BOUND"
    assert market.mechanics.fee_valid_until is not None
    provenance = market.original_metadata["fee_provenance"]
    if sport == "NBA" and now >= datetime(2026, 11, 6, 18, tzinfo=UTC):
        assert provenance["nba_account_tier"] == "UNKNOWN_UPPER_BOUND_TIER_1"
        assert provenance["nba_taker_theta_upper_bound"] == 0.095
        assert provenance["nba_taker_schedule_url"] == "https://docs.polymarket.us/fees"
    else:
        assert "nba_account_tier" not in provenance


def test_nba_fee_review_expires_fail_closed():
    result = attach_fees(
        SampleMarket(), datetime(2026, 11, 15, 4, 39, tzinfo=UTC), sport="NBA"
    )
    assert result.mechanics.fee_rate is None
    assert result.mechanics.fee_status == "UNVERIFIED"


def test_nba_unsupported_fee_market_scope_remains_blocked():
    result = attach_fees(
        SampleMarket(title="Table Tennis", description="Table Tennis"),
        datetime(2026, 11, 7, 15, tzinfo=UTC), sport="NBA",
    )
    assert result.mechanics.fee_status == "UNVERIFIED"
    assert result.mechanics.fee_rate is None
