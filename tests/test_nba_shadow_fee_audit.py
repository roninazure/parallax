"""Fee audit must never turn metadata into certified fees or BUY approval."""
import pytest

from scripts.nba_shadow_fee_audit import verify_metadata


def sources():
    return (
        {"ticker": "KXNBAGAME-26OCT26GSWDEN-GSW",
         "event_ticker": "KXNBAGAME-26OCT26GSWDEN",
         "status": "active",
         "fee_waiver_expiration_time": None},
        {"event_ticker": "KXNBAGAME-26OCT26GSWDEN",
         "series_ticker": "KXNBAGAME",
         "fee_type_override": None,
         "fee_multiplier_override": None},
        {"ticker": "KXNBAGAME", "fee_type": "quadratic",
         "fee_multiplier": 1},
        {"slug": "aec-nba-gs-den-2026-10-26",
         "id": 1386496, "marketType": "moneyline",
         "feeCoefficient": "0.0695", "status": "MARKET_STATUS_OPEN"},
    )


def test_audit_keeps_fees_uncertified_even_when_metadata_available():
    receipt = verify_metadata(*sources())
    assert receipt["buy_authorized"] is False
    assert receipt["net_edge_after_fees"] is None
    assert receipt["fee_certification"] == "PENDING_MARKET_BOUND_REVIEW"
    assert receipt["kalshi"]["series_fee_multiplier"] == 1
    assert receipt["polymarket_us"]["rebate_tier_verified"] is False


@pytest.mark.parametrize("index,key,value", [
    (0, "ticker", "KXNBAGAME-WRONG"),
    (0, "event_ticker", "WRONG"),
    (0, "status", "closed"),
    (1, "series_ticker", "KXNBAGAMEWRONG"),
    (2, "ticker", "KXNFLGAME"),
    (3, "slug", "aec-nba-wrong"),
    (3, "id", 100),
    (3, "marketType", "futures"),
    (3, "status", "MARKET_STATUS_CLOSED"),
])
def test_fee_metadata_identity_or_status_conflict_fails_closed(index, key, value):
    x = list(sources())
    x[index] = dict(x[index], **{key: value})
    with pytest.raises(ValueError):
        verify_metadata(*x)
