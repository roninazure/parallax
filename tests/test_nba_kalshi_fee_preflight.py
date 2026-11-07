"""NBA Kalshi fee metadata binding regression tests (no external requests)."""
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/nba_kalshi_fee_preflight.py"
spec = importlib.util.spec_from_file_location("nba_kalshi_fee_preflight", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
NOW = datetime(2026, 11, 5, 18, 0, tzinfo=UTC)

def fields():
    return (
        {"ticker": "KXNBAGAME-26NOV05ATLBOS-ATL", "event_ticker": "KXNBAGAME-26NOV05ATLBOS"},
        {"event_ticker": "KXNBAGAME-26NOV05ATLBOS", "series_ticker": "KXNBAGAME"},
        {"ticker": "KXNBAGAME", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1},
    )

def run(market=None, event=None, series=None, observed=None):
    m, e, s = fields()
    return module.inspect(m if market is None else market,
                          e if event is None else event,
                          s if series is None else series,
                          observed_at=observed or (NOW-timedelta(seconds=3)).isoformat(),
                          now=NOW)

def test_bound_metadata_never_authorizes_buy():
    result = run()
    assert result["metadata_consistent"] is True
    assert result["effective_fee_multiplier"] == 1
    assert result["published_schedule_verified"] is False
    assert result["buy_authorized"] is False
    assert result["publication_eligible"] is False

def test_mismatched_event_and_series_block():
    m, e, s = fields()
    e["event_ticker"] = "OTHER"
    s["ticker"] = "KXNHLGAME"
    r = run(m, e, s)
    assert {"EVENT_MISMATCH", "SERIES_MISMATCH"}.issubset(r["blockers"])

def test_market_injected_fee_override_blocks():
    m, e, s = fields()
    m["fee_dollars"] = "0"
    assert "UNVERIFIED_MARKET_FEE_OVERRIDE" in run(m, e, s)["blockers"]

def test_stale_and_unsupported_fee_type_block():
    m, e, s = fields()
    s["fee_type"] = "flat"
    r = run(m, e, s, (NOW-timedelta(minutes=3)).isoformat())
    assert "UNSUPPORTED_FEE_TYPE" in r["blockers"]
    assert "METADATA_STALE_OR_INVALID" in r["blockers"]

def test_event_multiplier_override_is_bounded_and_traced():
    m, e, s = fields()
    e["fee_multiplier_override"] = 0.5
    r = run(m, e, s)
    assert r["metadata_consistent"] is True
    assert r["effective_fee_multiplier"] == .5
    e["fee_multiplier_override"] = -1
    assert "INVALID_FEE_MULTIPLIER" in run(m, e, s)["blockers"]

def test_bad_market_identity_and_waiver_block():
    m, e, s = fields()
    m["ticker"] = "KXNHLGAME-26NOV05ATLBOS-ATL"
    m["fee_waiver_expiration_time"] = "2026-11-06T00:00:00Z"
    r = run(m, e, s)
    assert "NOT_NBA_GAME_MARKET" in r["blockers"]
    assert "UNVERIFIED_MARKET_FEE_OVERRIDE" in r["blockers"]
