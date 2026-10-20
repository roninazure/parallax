"""No-network regression tests for opening-day NBA identity probe."""
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from scripts import nba_opening_day_identity_probe as probe


def bdl_fixture(game_id, home, away, start):
    return {
        "id": game_id, "date": "2026-10-20", "season": 2026,
        "status_state": "scheduled", "postseason": False,
        "postponed": False, "datetime": start,
        "home_team": {"abbreviation": home},
        "visitor_team": {"abbreviation": away},
    }


def sample_bdl():
    return {
        "data": [
            bdl_fixture(250001, "NYK", "PHI", "2026-10-20T23:00:00Z"),
            bdl_fixture(250002, "SAS", "OKC", "2026-10-21T01:30:00Z"),
        ],
        "meta": {"next_cursor": None, "per_page": 100},
    }


def pmus_fixture(cfg):
    return [{
        "id": cfg["pmus_id"], "slug": cfg["pmus_slug"],
        "raw": {
            "id": int(cfg["pmus_id"]),
            "slug": cfg["pmus_slug"],
            "marketType": "moneyline", "status": "MARKET_STATUS_OPEN",
            "gameStartTime": cfg["start"].replace("+00:00", "Z"),
            "description": "Overtime is included if played.",
            "marketSides": [
                {"long": True, "team": {
                    "abbreviation": cfg["pmus_away"].lower(),
                    "league": "nba", "ordering": "away", "name": "Visitor",
                }},
                {"long": False, "team": {
                    "abbreviation": cfg["pmus_home"].lower(),
                    "league": "nba", "ordering": "home", "name": "Home",
                }},
            ],
        },
    }]


class ClientStub:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def markets_page(self, **kwargs):
        self.calls += 1
        assert kwargs["retry_transport_errors"] is False
        return self.rows


def test_bdl_verifies_two_authoritative_source_identities():
    checked = probe.verified_fixtures(sample_bdl())
    assert [x["fixture"].game_id for x in checked] == ["bdl:250001", "bdl:250002"]
    assert [x["fixture"].home_team for x in checked] == ["NYK", "SAS"]
    assert all(x["fixture"].season == 2026 for x in checked)


@pytest.mark.parametrize("mutation", [
    lambda data: data["data"].pop(),
    lambda data: data["meta"].update(next_cursor=12),
    lambda data: data["data"][0].update(status_state="final"),
    lambda data: data["data"][1].update(datetime="2026-10-21T01:00:00Z"),
    lambda data: data["data"][0].update(postseason=True),
])
def test_bdl_missing_or_invalid_scheduled_fixture_rejected(mutation):
    data = sample_bdl()
    mutation(data)
    with pytest.raises(ValueError, match="STOP|scheduled"):
        probe.verified_fixtures(data)


def test_kalshi_nested_event_market_is_not_lost(monkeypatch):
    cfg = probe.GAMES[0]
    market = {
        "event_ticker": cfg["kalshi_event"],
        "ticker": cfg["kalshi_event"] + "-PHI",
        "status": "active", "rules_primary": "Test NBA rule",
    }
    def fake_fetch(url):
        assert "with_nested_markets=true" in url
        return {
            "event": {"event_ticker": cfg["kalshi_event"], "markets": [market]},
            "markets": [],
        }

    monkeypatch.setattr(probe, "fetch_json", fake_fetch)
    receipt = probe.inspect_kalshi(cfg)
    assert len(receipt["markets"]) == 1
    assert receipt["markets"][0]["ticker"] == market["ticker"]


def test_kalshi_wrong_market_event_rejected(monkeypatch):
    cfg = probe.GAMES[0]
    monkeypatch.setattr(probe, "fetch_json", lambda url: {
        "event": {
            "event_ticker": cfg["kalshi_event"],
            "markets": [{"event_ticker": "KXNBAGAME-OTHER"}],
        }
    })
    with pytest.raises(ValueError, match="another event"):
        probe.inspect_kalshi(cfg)


@pytest.mark.parametrize("mutate", [
    lambda raw: raw.update(marketType="futures"),
    lambda raw: raw.update(gameStartTime="2026-10-20T22:00:00Z"),
    lambda raw: raw["marketSides"][0]["team"].update(league="wnba"),
    lambda raw: raw["marketSides"][0]["team"].update(abbreviation="ny"),
    lambda raw: raw["marketSides"][1].update(long=True),
])
def test_pmus_rejects_broken_identity_or_orientation(mutate):
    cfg = probe.GAMES[0]
    rows = pmus_fixture(cfg)
    mutate(rows[0]["raw"])
    with pytest.raises(ValueError, match="STOP"):
        probe.inspect_pmus(ClientStub(rows), cfg)


def test_pmus_accepts_explicit_nba_team_codes_and_yes_orientation():
    cfg = probe.GAMES[1]
    client = ClientStub(pmus_fixture(cfg))
    receipt = probe.inspect_pmus(client, cfg)
    assert receipt["slug"] == cfg["pmus_slug"]
    assert [x["ordering"] for x in receipt["outcomes"]] == ["away", "home"]
    assert [x["long_yes"] for x in receipt["outcomes"]] == [True, False]
    assert client.calls == 1
