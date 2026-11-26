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
