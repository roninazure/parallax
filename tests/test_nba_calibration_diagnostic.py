"""Offline tests for NBA diagnostic reporting; no historical file or network needed."""

from scripts.nba_calibration_diagnostic import summarize


def test_reliability_summary_reports_direction_of_calibration_gap():
    result = summarize([0.8, 0.7, 0.6, 0.9], [1, 0, 1, 0])
    assert result["games"] == 4
    assert abs(result["mean_predicted_home_win"] - 0.75) < 1e-12
    assert result["actual_home_win_rate"] == 0.5
    assert abs(result["calibration_gap"] - 0.25) < 1e-12
    assert 0 < result["brier"] < 1
    assert result["ece_10_bins"] > 0
