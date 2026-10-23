"""Offline bounded NHL research slate rotation and archive safety tests."""
from datetime import UTC, datetime, timedelta
import json
import sys

import pytest

from parallax.league_shadow_evidence import ShadowFixture
from scripts import nhl_shadow_slate_research as scan


NOW = datetime(2026, 10, 23, 18, tzinfo=UTC)


def fixture(n: int, *, hours: int):
    start = NOW + timedelta(hours=hours)
    return (
        start,
        f"KXNHLGAME-26OCT24GAME{n}",
        ShadowFixture(
            sport="NHL", game_id=f"nhl:{n}",
            season=2026, kickoff=start,
            home_team="CGY", away_team="TOR",
            venue_home_name="Calgary", venue_away_name="Toronto",
        ),
    )


def snapshot(path, when, *, screened=(), rejected=()):
    path.write_text(json.dumps({
        "mode": "RESEARCH_ONLY",
        "captured_at_utc": when.isoformat(),
        "screened": [{"event_ticker": ticker} for ticker in screened],
        "rejected": [{"event_ticker": ticker} for ticker in rejected],
    }), encoding="utf-8")


def test_rotation_prioritizes_unseen_games_and_nearest_start():
    matches = [
        fixture(1, hours=2),
        fixture(2, hours=4),
        fixture(3, hours=6),
        fixture(4, hours=8),
    ]
    selected = scan.rotate_slate(matches, {}, limit=2)
    assert [ticker for _, ticker, _ in selected] == [
        matches[0][1], matches[1][1],
    ]
    old = {ticker: NOW - timedelta(hours=2) for _, ticker, _ in selected}
    next_selected = scan.rotate_slate(matches, old, limit=2)
    assert [ticker for _, ticker, _ in next_selected] == [
        matches[2][1], matches[3][1],
    ]
    assert len(next_selected) == scan.MAX_GAMES


def test_all_seen_rotate_least_recent_first():
    matches = [fixture(i, hours=i + 1) for i in range(1, 5)]
    previous = {
        matches[0][1]: NOW - timedelta(minutes=2),
        matches[1][1]: NOW - timedelta(hours=3),
        matches[2][1]: NOW - timedelta(minutes=30),
        matches[3][1]: NOW - timedelta(hours=1),
    }
    selected = scan.rotate_slate(matches, previous, limit=2)
    assert [ticker for _, ticker, _ in selected] == [
        matches[1][1], matches[3][1],
    ]


def test_rotation_never_expands_request_budget():
    with pytest.raises(ValueError, match="out of bounds"):
        scan.rotate_slate([fixture(1, hours=2)], {}, limit=3)
    with pytest.raises(ValueError, match="out of bounds"):
        scan.rotate_slate([fixture(1, hours=2)], {}, limit=0)


def test_read_prior_successes_and_rejections(tmp_path):
    first = NOW - timedelta(hours=3)
    second = NOW - timedelta(hours=1)
    snapshot(
        tmp_path / "nhl-shadow-20261023T150000000001Z.json", first,
        screened=("KXNHLGAME-A",), rejected=("KXNHLGAME-B",),
    )
    snapshot(
        tmp_path / "nhl-shadow-20261023T170000000002Z.json", second,
        screened=("KXNHLGAME-B", "KXNHLGAME-C"),
    )
    prior = scan.past_screenings(tmp_path, now=NOW)
    assert prior == {
        "KXNHLGAME-A": first,
        "KXNHLGAME-B": second,
        "KXNHLGAME-C": second,
    }


def test_old_archive_snapshots_do_not_block_new_week(tmp_path):
    snapshot(
        tmp_path / "nhl-shadow-20261001T000000000001Z.json",
        NOW - timedelta(days=9),
        screened=("KXNHLGAME-OLD",),
    )
    assert scan.past_screenings(tmp_path, now=NOW) == {}


def test_bad_archives_fail_closed(tmp_path, monkeypatch):
    bad = tmp_path / "nhl-shadow-20261023T170000000001Z.json"
    bad.write_text("{broken", encoding="utf-8")
    with pytest.raises(scan.ResearchRunSkipped, match="snapshot malformed"):
        scan.past_screenings(tmp_path, now=NOW)

    # No NHL or Kalshi requests should be sent when the archive is corrupted.
    monkeypatch.setattr(scan, "RESEARCH_OUTPUT", tmp_path)
    monkeypatch.setattr(
        scan, "fetch_json", lambda url: pytest.fail("Unexpected public API call")
    )
    monkeypatch.setattr(sys, "argv", [
        "nhl_shadow_slate_research.py", "--max-games", "2",
    ])
    with pytest.raises(scan.ResearchRunSkipped, match="snapshot malformed"):
        scan.main()


def test_wrong_mode_and_future_snapshot_fail_closed(tmp_path):
    bad = tmp_path / "nhl-shadow-20261023T170000000001Z.json"
    bad.write_text(json.dumps({
        "mode": "PRODUCTION",
        "captured_at_utc": NOW.isoformat(),
        "screened": [],
        "rejected": [],
    }))
    with pytest.raises(scan.ResearchRunSkipped, match="snapshot malformed"):
        scan.past_screenings(tmp_path, now=NOW)
    snapshot(bad, NOW + timedelta(days=2), screened=("KXNHLGAME-A",))
    with pytest.raises(scan.ResearchRunSkipped, match="snapshot malformed"):
        scan.past_screenings(tmp_path, now=NOW)


def test_missing_archive_is_first_run(tmp_path):
    assert scan.past_screenings(tmp_path / "nonexistent", now=NOW) == {}
