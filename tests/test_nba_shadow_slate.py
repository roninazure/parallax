"""Offline NBA slate identity, pagination, rotation and archive safety tests."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json

import pytest

from parallax.nba_shadow_slate import (
    ShadowSlateError, nba_fixtures, matching_event_candidates,
    past_screenings, rotate_slate,
)

NOW = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)


def nba_game(*, game_id=123, home="CHA", away="MIA", home_city="Charlotte", away_city="Miami"):
    return {
        "id": game_id, "season": 2026, "date": "2026-10-24",
        "datetime": "2026-10-24T22:00:00Z", "postseason": False,
        "postponed": False, "status_state": "scheduled", "season_type": "regular",
        "ist_stage": None,
        "home_team": {"abbreviation": home, "city": home_city},
        "visitor_team": {"abbreviation": away, "city": away_city},
    }


def nba_page(*rows):
    return {"data": list(rows), "meta": {"next_cursor": None, "per_page": 100}}


def test_bdl_source_identity_produces_only_future_ticker_candidates():
    rows = nba_fixtures(nba_page(nba_game()), now=NOW, season=2026,
                        regular_season_query_verified=True)
    assert set(rows) == {"KXNBAGAME-26OCT24MIACHA"}
    fixture = rows["KXNBAGAME-26OCT24MIACHA"]
    assert (fixture.game_id, fixture.home_team, fixture.away_team) == ("bdl:123", "CHA", "MIA")
    assert fixture.venue_home_name == "Charlotte"


def test_authentic_regular_query_proof_is_mandatory():
    with pytest.raises(ShadowSlateError):
        nba_fixtures(nba_page(nba_game()), now=NOW, season=2026,
                     regular_season_query_verified=False)


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(meta={"next_cursor": 123}),
    lambda p: p["data"][0].update(season=2025),
    lambda p: p["data"][0].update(postseason=True),
    lambda p: p["data"][0]["home_team"].update(city=""),
    lambda p: p["data"][0].update(datetime="2026-10-24T21:00:00"),
    lambda p: p["data"].append(deepcopy(p["data"][0])),
])
def test_bad_source_or_duplicate_fixtures_fail_closed(mutate):
    page = nba_page(nba_game())
    mutate(page)
    with pytest.raises(ShadowSlateError):
        nba_fixtures(page, now=NOW, season=2026,
                     regular_season_query_verified=True)


def test_completed_postponed_or_started_games_are_ignored():
    completed = nba_game(game_id=1)
    completed["status_state"] = "final"
    postponed = nba_game(game_id=2)
    postponed["postponed"] = True
    started = nba_game(game_id=3)
    started["datetime"] = "2026-10-22T22:00:00Z"
    started["date"] = "2026-10-22"
    assert nba_fixtures(nba_page(completed, postponed, started), now=NOW, season=2026,
                        regular_season_query_verified=True) == {}


def test_matching_events_are_candidates_not_trade_authorization():
    fixtures = nba_fixtures(nba_page(nba_game()), now=NOW, season=2026,
                            regular_season_query_verified=True)
    events = {"events": [{"series_ticker": "KXNBAGAME", "event_ticker": "KXNBAGAME-26OCT24MIACHA"}]}
    selected = matching_event_candidates(fixtures, events)
    assert [row[1] for row in selected] == ["KXNBAGAME-26OCT24MIACHA"]
    assert matching_event_candidates(fixtures, {"events": []}) == []
    with pytest.raises(ShadowSlateError):
        matching_event_candidates(fixtures, {"events": events["events"] * 2})
    with pytest.raises(ShadowSlateError):
        matching_event_candidates(fixtures, {"events": [{"series_ticker": "KXNHLGAME", "event_ticker": "KXNBAGAME-26OCT24MIACHA"}]})
    with pytest.raises(ShadowSlateError):
        matching_event_candidates(fixtures, {"events": events["events"], "cursor": "next"})


def test_rotation_prioritizes_unseen_then_least_recent():
    fixture = nba_fixtures(nba_page(nba_game()), now=NOW, season=2026,
                           regular_season_query_verified=True)["KXNBAGAME-26OCT24MIACHA"]
    matches = [(NOW+timedelta(hours=i), f"KXNBAGAME-{i}", fixture) for i in range(4)]
    prior = {"KXNBAGAME-0": NOW-timedelta(days=1), "KXNBAGAME-2": NOW-timedelta(days=2)}
    assert [x[1] for x in rotate_slate(matches, prior, limit=2)] == ["KXNBAGAME-1", "KXNBAGAME-3"]
    for invalid in (0, 3, True):
        with pytest.raises(ShadowSlateError):
            rotate_slate(matches, prior, limit=invalid)


def test_archive_fails_closed_on_corruption_and_remembers_valid_receipts(tmp_path):
    ticker = "KXNBAGAME-26OCT24MIACHA"
    file = tmp_path / "nba-shadow-1.json"
    receipt = {
        "mode": "RESEARCH_ONLY", "sport": "NBA", "buy_authorized": False,
        "captured_at_utc": NOW.isoformat(),
        "screened": [{"event_ticker": ticker}], "rejected": [],
    }
    file.write_text(json.dumps(receipt))
    assert past_screenings(tmp_path, now=NOW)[ticker] == NOW
    file.write_text("broken json")
    with pytest.raises(ShadowSlateError):
        past_screenings(tmp_path, now=NOW)
