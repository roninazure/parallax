"""Official NBA schedule text importer: offline provenance and safety tests."""
from datetime import date

import pytest

from scripts.nba_official_schedule_extract import extract


def test_parses_historical_games_only_and_marks_uncertainty():
    text = """GAME DAY DATE TEAM 1 TEAM 2 LOCAL ET NAT TV R #
6 Thu. 10/22/26 Cleveland at Philadelphia 7:00 PM 7:00 PM ESPN
7 Thu. 10/22/26 Denver at Oklahoma City 8:30 PM 9:30 PM ESPN
8 Fri. 10/23/26 New York at Boston 7:00 PM 7:00 PM Prime
9 Fri. 10/23/26 Houston at San Antonio 8:30 PM 9:30 PM Prime A
"""
    rows, provenance = extract(text, date(2026, 10, 22), date(2026, 10, 22))
    assert len(rows) == 2
    assert { (r["away_team"], r["home_team"]) for r in rows } == {
        ("CLE", "PHI"), ("DEN", "OKC")}
    assert provenance["subject_to_change"] is True
    assert provenance["historical_expected_final_status_inferred"] is True
    assert provenance["source_authorization_verified"] is False
    assert provenance["network_requests"] == 0


def test_unknown_team_and_duplicate_fail_closed():
    row = "6 Thu. 10/22/26 Cleveland at Philadelphia 7:00 PM 7:00 PM ESPN"
    with pytest.raises(ValueError, match="duplicate"):
        extract(row + "\n" + row, date(2026, 10, 22), date(2026, 10, 22))
    with pytest.raises(ValueError, match="unrecognized"):
        extract("6 Thu. 10/22/26 Cleveland at Unrecognized 7:00 PM 7:00 PM",
                date(2026, 10, 22), date(2026, 10, 22))


def test_window_is_bounded_and_empty_fails():
    with pytest.raises(ValueError, match="unexpected bounded"):
        extract("irrelevant", date(2026, 10, 22), date(2027, 2, 1))
    with pytest.raises(ValueError, match="no valid"):
        extract("not an NBA schedule", date(2026, 10, 22), date(2026, 10, 31))
