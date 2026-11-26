from datetime import UTC, datetime, timedelta

import pytest

from integrations.parallax_live.cfb_v02_adapter import convert_cfb_feed


NOW = datetime(2026, 11, 26, 2, 14, 10, tzinfo=UTC)


def feed():
    row = {
        "action": "BUY", "publication_eligible": True, "sport": "CFB",
        "status": "CURRENT", "freshness": "FRESH", "venue": "KALSHI",
        "selected_team": "Ohio", "signal_id": "px1_verified123",
        "updated_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "resolution_time": (NOW + timedelta(days=1)).isoformat(),
        "price": 0.41, "model_probability": 0.57, "edge_pp": 16.0,
    }
    return {
        "read_only": True, "lane": "CFB", "runtime_state": "LIVE",
        "generated_at": NOW.isoformat(), "source_as_of": NOW.isoformat(),
        "plays": [row],
    }


def test_certified_buy_maps_price_and_selection():
    result = convert_cfb_feed(feed(), now=NOW)
    assert result["plays"][0]["selection"] == "Ohio"
    assert result["plays"][0]["price_cents"] == 41.0
    assert result["read_only"] is True and result["execution_enabled"] is False
    assert result["source_state"] == "LIVE"


def test_missing_team_suppresses_buy():
    payload = feed()
    del payload["plays"][0]["selected_team"]
    assert convert_cfb_feed(payload, now=NOW)["plays"] == []


def test_uncertified_buy_suppressed():
    payload = feed()
    payload["plays"][0]["publication_eligible"] = False
    assert convert_cfb_feed(payload, now=NOW)["plays"] == []


def test_future_quote_suppressed():
    payload = feed()
    payload["plays"][0]["updated_at"] = (NOW + timedelta(minutes=5)).isoformat()
    assert convert_cfb_feed(payload, now=NOW)["plays"] == []


def test_stale_feed_blocks_conversion():
    payload = feed()
    payload["generated_at"] = (NOW - timedelta(hours=2)).isoformat()
    with pytest.raises(ValueError, match="stale"):
        convert_cfb_feed(payload, now=NOW)


def test_other_lane_rejected():
    payload = feed()
    payload["lane"] = "NFL"
    with pytest.raises(ValueError, match="CFB"):
        convert_cfb_feed(payload, now=NOW)


@pytest.mark.parametrize("runtime_state", ["UNKNOWN", "PAUSED", "STALE", "RUNNING", None])
def test_unverified_runtime_state_fails_closed(runtime_state):
    payload = feed()
    if runtime_state is None:
        del payload["runtime_state"]
    else:
        payload["runtime_state"] = runtime_state
    with pytest.raises(ValueError, match="verified LIVE"):
        convert_cfb_feed(payload, now=NOW)
