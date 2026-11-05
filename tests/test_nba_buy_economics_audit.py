"""Offline integration of frozen NBA forecasts with executable quote economics."""
import importlib.util
import json
from pathlib import Path
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/nba_buy_economics_audit.py"
spec = importlib.util.spec_from_file_location("nba_buy_economics_audit", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def inputs(tmp_path):
    forecasts = tmp_path / "forward"
    quotes = tmp_path / "quotes"
    forecasts.mkdir()
    quotes.mkdir()
    (forecasts / "nba-shadow-a.json").write_text(json.dumps({
        "captured_at_utc": "2026-11-04T14:00:00+00:00",
        "screened": [{"matchup": "ATL@WAS",
                      "start_utc": "2026-11-05T01:00:00+00:00",
                      "raw_elo_home_probability": .8}]}))
    return forecasts, quotes

def quote(*, fee_status="VERIFIED_UPPER_BOUND", ask=.60, captured="2026-11-04T15:00:02+00:00",
          observed="2026-11-04T15:00:00+00:00", selection="WSH"):
    return {
        "sport": "NBA", "game_id": "bdl:1", "home_team": "WAS",
        "away_team": "ATL", "selected_yes_team": selection,
        "venue": "kalshi", "market_id": "KXNBAGAME-TEST-WAS",
        "scheduled_start": "2026-11-05T01:00:00+00:00",
        "source_observed_at": observed, "captured_at": captured,
        "rules_sha256": "a"*64, "status": "OPEN",
        "yes_ask": ask, "no_ask": .42,
        "fee_status": fee_status,
        "fee_scenario_25": {
            "YES": {"available": True, "contracts": 40, "fees_estimate": 1.0, "total_cost": 25.0},
            "NO": {"available": True, "contracts": 55, "fees_estimate": 1.0, "total_cost": 24.1}
        },
    }

def save(quotes, row):
    (quotes/"parallax-league-quotes-20261104.jsonl").write_text(json.dumps(row)+"\n")

def test_verified_fees_can_pass_economics_but_never_buy(tmp_path):
    forecasts, quotes = inputs(tmp_path)
    save(quotes, quote())
    report = module.audit(forecasts, quotes)
    assert report["historical_economic_passes"] == 1
    yes = next(r for r in report["rows"] if r["side"] == "YES")
    assert yes["selected_team"] == "WAS"
    assert yes["model_probability"] == pytest.approx(.8)
    assert yes["gross_edge"] == pytest.approx(.2)
    assert yes["net_ev_25"] == pytest.approx(7)
    assert yes["economic_check"] == "PASSED_HISTORICAL_ONLY"
    assert yes["buy_authorized"] is False
    assert report["publication_eligible"] is False

def test_unverified_kalshi_nba_fees_block_even_positive_gross_edge(tmp_path):
    forecasts, quotes = inputs(tmp_path)
    save(quotes, quote(fee_status="UNVERIFIED"))
    report = module.audit(forecasts, quotes)
    assert report["historical_economic_passes"] == 0
    assert report["failure_counts"]["FEES_UNVERIFIED"] == 2
    yes = next(r for r in report["rows"] if r["side"] == "YES")
    assert yes["net_ev_25"] is None
    assert yes["buy_authorized"] is False

def test_stale_book_and_future_forecast_rejected(tmp_path):
    forecasts, quotes = inputs(tmp_path)
    save(quotes, quote(captured="2026-11-04T15:02:00+00:00",
                       observed="2026-11-04T13:00:00+00:00"))
    report = module.audit(forecasts, quotes)
    assert report["historical_economic_passes"] == 0
    assert report["failure_counts"]["STALE_AT_CAPTURE"] == 2
    assert report["failure_counts"]["FORECAST_NOT_AVAILABLE_AT_QUOTE_TIME"] == 2

def test_no_network_or_side_effects_and_row_limit(tmp_path):
    forecasts, quotes = inputs(tmp_path)
    save(quotes, quote())
    with pytest.raises(ValueError, match="budget"):
        module.audit(forecasts, quotes, max_rows=0)
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == [
        "nba-shadow-a.json", "parallax-league-quotes-20261104.jsonl"
    ]

def test_rejects_untrusted_quote_file_symlink(tmp_path):
    forecasts, quotes = inputs(tmp_path)
    external = tmp_path / "outside.jsonl"
    external.write_text(json.dumps(quote())+"\n")
    (quotes / "parallax-league-quotes-20261104.jsonl").symlink_to(external)
    with pytest.raises(ValueError, match="symlink"):
        module.audit(forecasts, quotes)
