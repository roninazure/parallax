"""Public CFB BUY selection must be explicit and not inferred from YES/NO."""
from parallax.public_feed import _public_play


def test_cfb_buy_exports_verified_selection_only():
    row = {
        "venue": "KALSHI", "market_id": "KXNCAAFGAME-TEST-OHIO",
        "side": "YES", "verdict": "BUY", "publication_eligible": True,
        "selected_team": "Ohio", "matchup": "Toledo at Ohio",
    }
    result = _public_play(row, "cfb")
    assert result["selected_team"] == "Ohio"
    assert result["action"] == "BUY"


def test_cfb_buy_does_not_infer_team_from_market_ticker():
    row = {
        "venue": "KALSHI", "market_id": "KXNCAAFGAME-TEST-TOL",
        "side": "NO", "verdict": "BUY", "publication_eligible": True,
        "matchup": "Toledo at Ohio",
    }
    result = _public_play(row, "cfb")
    assert "selected_team" not in result


def test_other_sports_do_not_gain_cfb_selected_team_field():
    row = {
        "venue": "KALSHI", "market_id": "NFL-TEST",
        "side": "YES", "verdict": "BUY", "publication_eligible": True,
        "selected_team": "Ohio",
    }
    result = _public_play(row, "nfl")
    assert "selected_team" not in result


def test_cfb_equivalent_yes_no_contracts_publish_one_economic_selection(monkeypatch):
    """YES Ohio and NO Toledo select the same Ohio win; keep cheaper quote."""
    from datetime import UTC, datetime
    import parallax.public_feed as public_feed

    monkeypatch.setattr(public_feed, "_cfb_buy_source_eligible", lambda row, now: row.get("eligible", False))
    rows = [
        {"eligible": True, "economic_key": "CFB:game1:OHIO", "venue": "KALSHI",
         "market_id": "KXNCAAFGAME-TEST-TOL", "side": "NO", "selected_team": "Ohio",
         "executable_price": .41, "raw_edge": 16.4, "liquidity": 100},
        {"eligible": True, "economic_key": "CFB:game1:OHIO", "venue": "KALSHI",
         "market_id": "KXNCAAFGAME-TEST-OHIO", "side": "YES", "selected_team": "Ohio",
         "executable_price": .40, "raw_edge": 17.4, "liquidity": 80},
        {"eligible": True, "economic_key": "CFB:game1:TOLEDO", "venue": "KALSHI",
         "market_id": "KXNCAAFGAME-TEST-TOL", "side": "YES", "selected_team": "Toledo",
         "executable_price": .60, "raw_edge": 10.0, "liquidity": 100},
        {"eligible": False, "economic_key": "CFB:game1:OHIO", "venue": "KALSHI",
         "market_id": "KXNCAAFGAME-INVALID", "side": "NO", "selected_team": "Ohio",
         "executable_price": .01, "raw_edge": 99.0, "liquidity": 100},
    ]
    chosen = public_feed._eligible_cfb_buy_indexes(rows, datetime(2026, 11, 26, tzinfo=UTC))
    assert chosen == {1, 2}


def test_cfb_equivalent_contract_tie_break_is_deterministic(monkeypatch):
    from datetime import UTC, datetime
    import parallax.public_feed as public_feed

    monkeypatch.setattr(public_feed, "_cfb_buy_source_eligible", lambda row, now: True)
    rows = [
        {"economic_key": "CFB:game2:TCU", "venue": "KALSHI", "market_id": "TTU",
         "side": "NO", "executable_price": .14, "raw_edge": 14.82, "liquidity": 100},
        {"economic_key": "CFB:game2:TCU", "venue": "KALSHI", "market_id": "TCU",
         "side": "YES", "executable_price": .14, "raw_edge": 14.82, "liquidity": 100},
    ]
    chosen = public_feed._eligible_cfb_buy_indexes(rows, datetime(2026, 11, 26, tzinfo=UTC))
    assert chosen == {1}
