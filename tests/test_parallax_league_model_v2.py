import hashlib
import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from parallax.league_elo_experimental import FinalGame
from parallax.league_model import verify_artifact
from parallax.league_model_v2 import (
    FEATURE_NAMES,
    _features,
    _date_isolated_selection_split,
    _paired_delta_intervals,
    _canonical,
    forecast_features_v2,
    verify_artifact_v2,
    reconcile_independent_history,
    train_artifact_v2,
)
from parallax.league_quote_snapshots import LeagueQuoteSnapshotStore
from parallax.models import Mechanics, NormalizedMarket, Venue


def _games():
    rows = []
    start = date(2024, 10, 1)
    for day in range(120):
        for i in range(12):
            home, away = f"H{i}", f"A{i}"
            winner = home if (day + i) % 5 != 0 else away
            rows.append(
                FinalGame(
                    "NBA",
                    f"g-{day}-{i}",
                    2024,
                    start + timedelta(days=day),
                    home,
                    away,
                    winner,
                )
            )
    return rows


def test_v2_feature_generation_is_pre_date_and_rejects_future_results():
    games = _games()
    before = forecast_features_v2(
        games,
        sport="NBA",
        game_date=date(2025, 2, 1),
        season=2024,
        home_team="H1",
        away_team="A1",
        allowed_teams={f"H{i}" for i in range(12)} | {f"A{i}" for i in range(12)},
    )
    with pytest.raises(ValueError, match="same-day or future"):
        forecast_features_v2(
            [
                *games,
                FinalGame("NBA", "future", 2024, date(2025, 2, 1), "H1", "A1", "H1"),
            ],
            sport="NBA",
            game_date=date(2025, 2, 1),
            season=2024,
            home_team="H1",
            away_team="A1",
            allowed_teams={f"H{i}" for i in range(12)} | {f"A{i}" for i in range(12)},
        )
    assert len(before) == len(FEATURE_NAMES)


def test_v2_model_selection_never_divides_one_et_game_date():
    # A row-count split at 80% would bisect a 12-game date in this fixture.
    training = _games()[:72 * 12]
    split = _date_isolated_selection_split(training)
    assert 0 < split < len(training)
    assert training[split - 1].game_date < training[split].game_date
    assert split == 57 * 12


def test_v2_training_is_deterministic_and_marks_old_holdout_exploratory():
    first = train_artifact_v2(_games(), sport="NBA", source_sha256="a" * 64)
    second = train_artifact_v2(_games(), sport="NBA", source_sha256="a" * 64)
    assert first["artifact_sha256"] == second["artifact_sha256"]
    assert first["splits"]["holdout_status"] == "EXPLORATORY_PREVIOUSLY_INSPECTED"
    assert first["certified"] is False
    selection = first["selection"]
    assert date.fromisoformat(selection["fit_end"]) < date.fromisoformat(
        selection["validation_start"]
    )
    assert selection["fit_games"] + selection["validation_games"] == (
        first["splits"]["train_games"]
    )
    assert first["holdout_confidence_intervals"]["method"] == "ET-date-block-bootstrap"


def test_v2_later_holdout_is_distinct_and_required_for_certification():
    history = _games()
    start = max(g.game_date for g in history) + timedelta(days=1)
    forward = [
        FinalGame(
            "NBA",
            f"forward-{day}-{i}",
            2024,
            start + timedelta(days=day),
            f"H{i}",
            f"A{i}",
            f"H{i}" if (day + i) % 5 else f"A{i}",
        )
        for day in range(3)
        for i in range(12)
    ]
    artifact = train_artifact_v2(
        history,
        sport="NBA",
        source_sha256="a" * 64,
        forward_holdout=forward,
        forward_holdout_source_sha256="c" * 64,
        independent_source_sha256="b" * 64,
        independent_games=history,
    )
    assert artifact["splits"]["holdout_status"] == "FRESH_INDEPENDENT"
    assert artifact["acceptance_gates"]["fresh_forward_holdout"] is True
    assert artifact["acceptance_gates"]["independent_source_verified"] is True
    assert artifact["certified"] is False



def test_v2_verifier_rejects_rehashed_missing_or_duplicate_source_evidence():
    # Rehashing an artifact cannot manufacture a genuinely distinct provider
    # or a SHA-pinned fresh forward holdout.
    history = _games()
    start = max(g.game_date for g in history) + timedelta(days=1)
    forward = [
        FinalGame("NBA", "forward", 2024, start, "H0", "A0", "H0"),
    ]
    artifact = train_artifact_v2(
        history, sport="NBA", source_sha256="a" * 64,
        forward_holdout=forward, forward_holdout_source_sha256="c" * 64,
        independent_source_sha256="b" * 64, independent_games=history,
    )
    assert artifact["acceptance_gates"]["independent_source_verified"] is True
    assert artifact["acceptance_gates"]["fresh_forward_holdout"] is True
    for field, replacement in (
        ("independent_source_sha256", "A" * 64),
        ("forward_holdout_source_sha256", None),
    ):
        tampered = deepcopy(artifact)
        tampered[field] = replacement
        unsigned = {k: v for k, v in tampered.items() if k != "artifact_sha256"}
        tampered["artifact_sha256"] = hashlib.sha256(_canonical(unsigned)).hexdigest()
        with pytest.raises(ValueError, match="gates do not match recorded evidence"):
            verify_artifact_v2(
                tampered, sport="NBA", now=datetime(2026, 10, 30, tzinfo=UTC),
                expected_sha256=None, require_certified=False,
            )


def test_v2_rejects_duplicate_primary_fixture_with_distinct_provider_ids():
    history = _games()
    first = history[0]
    duplicate = FinalGame(
        first.sport, "other-provider-id", first.season, first.game_date,
        first.home_team, first.away_team, first.winner_team,
    )
    with pytest.raises(ValueError, match="training history contains duplicate fixture"):
        train_artifact_v2([*history, duplicate], sport="NBA", source_sha256="a" * 64)


def test_v2_rejects_duplicate_forward_fixture_with_distinct_provider_ids():
    history = _games()
    day = max(g.game_date for g in history) + timedelta(days=1)
    first = FinalGame("NBA", "forward-1", 2024, day, "H0", "A0", "H0")
    duplicate = FinalGame("NBA", "forward-2", 2024, day, "H0", "A0", "H0")
    with pytest.raises(ValueError, match="forward holdout contains duplicate fixture"):
        train_artifact_v2(
            history, sport="NBA", source_sha256="a" * 64,
            forward_holdout=[first, duplicate],
            forward_holdout_source_sha256="b" * 64,
        )


def test_v2_rejects_noncanonical_forward_holdout_teams():
    history = _games()
    day = max(g.game_date for g in history) + timedelta(days=1)
    noncanonical = FinalGame("NBA", "forward", 2024, day, "h0", "A0", "h0")
    with pytest.raises(ValueError, match="forward holdout team identities"):
        train_artifact_v2(
            history, sport="NBA", source_sha256="a" * 64,
            forward_holdout=[noncanonical],
            forward_holdout_source_sha256="b" * 64,
        )



def test_v2_season_boundary_resets_prior_form_and_unknown_team_fails_closed():
    games = _games()[:12]
    first_date = games[0].game_date
    new_season = FinalGame(
        "NBA", "new-season", 2025, first_date + timedelta(days=200), "H0", "A0", "H0"
    )
    no_history = FinalGame(
        "NBA", "baseline", 2025, first_date + timedelta(days=200), "H0", "A0", "H0"
    )
    assert (
        _features([*games, new_season], sport="NBA")[("NBA", new_season.game_id)]
        == _features([no_history], sport="NBA")[("NBA", "baseline")]
    )
    with pytest.raises(ValueError, match="absent from verified"):
        forecast_features_v2(
            games,
            sport="NBA",
            game_date=date(2025, 3, 1),
            season=2024,
            home_team="NEW",
            away_team="A0",
            allowed_teams={g.home_team for g in games} | {g.away_team for g in games},
        )


@pytest.mark.parametrize(
    "sport,filename,count",
    [
        ("NBA", "nba_v2_candidate.json", 2460),
        ("NHL", "nhl_v2_candidate.json", 5248),
    ],
)
def test_v2_real_history_artifacts_are_exploratory_and_fail_closed(
    sport, filename, count
):
    path = (
        Path(__file__).resolve().parents[1]
        / "reports"
        / "nba_nhl_model_candidates"
        / filename
    )
    artifact = json.loads(path.read_text())
    assert artifact["model_version"] == "parallax-league-logistic-form-v2"
    assert artifact["source_game_count"] == count
    assert artifact["splits"]["same_day_isolated"] is True
    assert artifact["splits"]["holdout_status"] == "EXPLORATORY_PREVIOUSLY_INSPECTED"
    assert artifact["holdout_confidence_intervals"]["replicates"] == 400
    assert artifact["certified"] is False
    verify_artifact(
        artifact,
        sport=sport,
        require_certified=False,
        now=datetime(2026, 10, 29, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="exploratory|uncertified"):
        verify_artifact(
            artifact,
            sport=sport,
            expected_sha256=artifact["artifact_sha256"],
            now=datetime(2026, 10, 29, tzinfo=UTC),
        )


def test_quote_snapshot_store_records_sanitized_executable_depth_and_retains(tmp_path):
    store = LeagueQuoteSnapshotStore(tmp_path / "quotes", retention_days=30)
    fixture = SimpleNamespace(
        game_id="g1",
        kickoff=datetime(2026, 10, 30, 0, 0, tzinfo=UTC),
        home_team="H1",
        away_team="A1",
    )
    market = NormalizedMarket(
        venue=Venue.KALSHI,
        venue_market_id="ticker",
        slug="ticker",
        title="Winner",
        description="",
        category="sports",
        event="event",
        outcomes={"YES": "H1", "NO": "A1"},
        resolution_rules="full game winner",
        resolution_time=None,
        status="OPEN",
        yes_bid=0.48,
        yes_ask=0.52,
        no_bid=0.47,
        no_ask=0.53,
        best_bid_size=10,
        best_ask_size=9,
        executable_depth={"YES": ((0.52, 9),), "NO": ((0.53, 8),)},
        recent_volume=None,
        recent_trade_count=None,
        last_trade_time=None,
        book_timestamp="2026-10-29T23:59:00+00:00",
        data_timestamp="2026-10-29T23:59:00+00:00",
        source_url=None,
        mechanics=Mechanics(fee_rate=0.07, fee_status="VERIFIED_UPPER_BOUND"),
    )
    path = store.record(
        sport="NBA",
        fixture=fixture,
        market=market,
        observed_at="2026-10-29T23:59:00+00:00",
        yes_team="H1",
    )
    row = json.loads(path.read_text().strip())
    assert row["executable_depth"]["YES"] == [[0.52, 9.0]]
    assert row["rules_sha256"]
    assert "raw" not in row and "api_key" not in row


def test_independent_provider_reconciliation_ignores_ids_and_order():
    primary = _games()[:12]
    secondary = [
        FinalGame(g.sport, f"provider-b-{i}", g.season, g.game_date,
                  g.home_team, g.away_team, g.winner_team)
        for i, g in enumerate(reversed(primary))
    ]
    assert len(reconcile_independent_history(primary, secondary, sport="NBA")) == 64


def test_independent_provider_reconciliation_rejects_wrong_winner_and_missing_game():
    primary = _games()[:12]
    secondary = [
        FinalGame(g.sport, f"other-{i}", g.season, g.game_date,
                  g.home_team, g.away_team, g.winner_team)
        for i, g in enumerate(primary)
    ]
    with pytest.raises(ValueError, match="do not reconcile"):
        reconcile_independent_history(primary, secondary[:-1], sport="NBA")
    bad = list(secondary)
    g = bad[0]
    bad[0] = FinalGame(g.sport, g.game_id, g.season, g.game_date,
                       g.home_team, g.away_team,
                       g.away_team if g.winner_team == g.home_team else g.home_team)
    with pytest.raises(ValueError, match="do not reconcile"):
        reconcile_independent_history(primary, bad, sport="NBA")


def test_independent_provider_reconciliation_rejects_duplicate_fixture_different_ids():
    primary = _games()[:12]
    secondary = [
        FinalGame(g.sport, f"other-{i}", g.season, g.game_date,
                  g.home_team, g.away_team, g.winner_team)
        for i, g in enumerate(primary)
    ]
    duplicate = secondary[0]
    secondary.append(FinalGame(
        duplicate.sport, "different-provider-id", duplicate.season,
        duplicate.game_date, duplicate.home_team, duplicate.away_team,
        duplicate.winner_team))
    with pytest.raises(ValueError, match="duplicate independent fixture"):
        reconcile_independent_history(primary, secondary, sport="NBA")


def test_paired_calibration_intervals_identical_predictions_have_zero_delta():
    dates = [date(2025, 10, 1), date(2025, 10, 1),
             date(2025, 10, 2), date(2025, 10, 3)]
    candidate = [0.15, 0.8, 0.65, 0.25]
    outcome = [0, 1, 1, 0]
    result = _paired_delta_intervals(candidate, candidate, outcome, dates)
    assert result["method"] == "paired-ET-date-block-bootstrap"
    assert result["n_blocks"] == 3
    assert result["replicates"] == 400
    assert all(bounds == [0.0, 0.0] for bounds in result["intervals"].values())


def test_paired_calibration_intervals_preserve_direction_for_strict_improvement():
    dates = [date(2025, 10, 1) + timedelta(days=i // 2) for i in range(24)]
    outcome = [i % 2 for i in range(24)]
    candidate = [0.9 if y else 0.1 for y in outcome]
    baseline = [0.5] * len(outcome)
    result = _paired_delta_intervals(candidate, baseline, outcome, dates)
    assert result["n_blocks"] == 12
    for bounds in result["intervals"].values():
        assert bounds[1] < 0, bounds


def test_paired_calibration_intervals_reject_unaligned_samples():
    with pytest.raises(ValueError, match="must align"):
        _paired_delta_intervals([0.4], [0.5, 0.5], [1], [date(2025, 10, 1)])


def test_v2_candidate_includes_paired_uncertainty_without_certification():
    artifact = train_artifact_v2(_games(), sport="NBA", source_sha256="a" * 64)
    intervals = artifact["paired_baseline_delta_intervals"]
    assert set(intervals) == {
        "candidate_minus_50_50",
        "candidate_minus_constant_home_rate",
        "candidate_minus_elo_calibrated",
    }
    assert all(item["replicates"] == 400 for item in intervals.values())
    assert artifact["certified"] is False
