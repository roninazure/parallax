"""Offline checks for bounded NBA dual-venue research report.

No provider requests, real money, production files, or trading.
"""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from parallax.models import Action, Side
from scripts import nba_dual_venue_shadow_snapshot as snapshot


def fake_market(*, status="OPEN", fee_status="UNVERIFIED"):
    return SimpleNamespace(
        status=status,
        yes_bid=0.11,
        yes_ask=0.88,
        no_bid=0.12,
        no_ask=0.89,
        executable_depth={
            Side.YES: ((0.88, 212.0),),
            Side.NO: ((0.89, 209.0),),
        },
        mechanics=SimpleNamespace(fee_status=fee_status),
    )


@pytest.mark.parametrize("side,team,bid,ask,size,margin", [
    (Side.YES, "GSW", 0.11, 0.88, 212.0, -0.7067),
    (Side.NO, "DEN", 0.12, 0.89, 209.0, -0.0633),
])
def test_shadow_quotes_do_not_claim_net_profit(
    monkeypatch, side, team, bid, ask, size, margin,
):
    monkeypatch.setattr(
        snapshot, "evaluate_shadow",
        lambda *args, **kwargs: SimpleNamespace(
            suggested_action=Action.PASS,
            verdict=SimpleNamespace(failed_gates=("fees_unknown", "confidence")),
        ),
    )
    evidence = SimpleNamespace(fair_probability=0.1733)
    result = snapshot.quote_row(
        fake_market(), evidence, object(), object(),
        side, datetime(2026, 10, 20, tzinfo=UTC),
    )
    assert result["outcome"] == team
    assert result["bid"] == bid
    assert result["ask"] == ask
    assert result["available_at_ask"] == size
    assert result["nominal_edge_before_costs"] == pytest.approx(margin)
    assert result["net_edge_after_fees"] is None
    assert result["fee_status"] == "UNVERIFIED"
    assert result["shadow_action"] == "PASS"
    assert result["buy_authorized"] is False


def test_missing_evidence_fails_closed(monkeypatch):
    monkeypatch.setattr(
        snapshot, "evaluate_shadow", lambda *args, **kwargs: None
    )
    with pytest.raises(ValueError, match="Unverified market/evidence"):
        snapshot.quote_row(
            fake_market(), None, object(), object(), Side.YES,
            datetime(2026, 10, 20, tzinfo=UTC),
        )


def test_closed_market_fails_before_fees_or_evidence(monkeypatch):
    def must_not_call(*args, **kwargs):
        raise AssertionError("should not examine closed market")

    monkeypatch.setattr(snapshot, "shadow_evidence", must_not_call)
    monkeypatch.setattr(snapshot, "attach_fees", must_not_call)
    with pytest.raises(ValueError, match="market not open"):
        snapshot.venue_report(
            fake_market(status="CLOSED"), object(), object(),
            datetime(2026, 10, 20, tzinfo=UTC),
        )


def test_no_retry_in_kalshi_fetch_on_rate_limit(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("429")

    monkeypatch.setattr(snapshot, "urlopen", fail)
    with pytest.raises(RuntimeError, match="429"):
        snapshot.kalshi_json(
            "https://external-api.kalshi.com/trade-api/v2/markets/example"
        )
    assert len(calls) == 1
