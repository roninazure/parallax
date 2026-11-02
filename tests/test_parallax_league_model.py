import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from parallax.league_elo_experimental import FinalGame
from parallax.league_model import (
    train_artifact,
    verify_artifact,
    verify_history_snapshot,
)


def _history():
    rows = []
    first = date(2024, 10, 1)
    for day in range(60):
        for matchup in range(25):
            home = f"H{matchup}"
            away = f"A{matchup}"
            home_won = (day + matchup) % 3 != 0
            rows.append(
                FinalGame(
                    "NBA",
                    f"g-{day}-{matchup}",
                    2024,
                    first + timedelta(days=day * 3),
                    home,
                    away,
                    home if home_won else away,
                )
            )
    return rows


def test_fit_artifact_has_disjoint_chronological_splits_and_reproducible_hash():
    games = _history()
    kwargs = {
        "sport": "NBA",
        "source_sha256": "a" * 64,
        "independent_source_sha256": "b" * 64,
        "source_authorization_reference": "review://model-data-approval",
        "model_owner_approval_reference": "review://criteria-approval",
    }
    a = train_artifact(games, **kwargs)
    b = train_artifact(games, **kwargs)
    assert a["artifact_sha256"] == b["artifact_sha256"]
    splits = a["splits"]
    assert splits["train_end"] < splits["calibration_start"]
    assert splits["calibration_end"] < splits["holdout_start"]
    assert splits["same_day_isolated"] is True
    assert a["source_game_count"] == 1500
    assert a["holdout_metrics"]["n_games"] == splits["holdout_games"]
    assert a["baseline_50_50"]["brier"] == 0.25
    verify_artifact(
        a, sport="NBA", require_certified=False, now=datetime(2025, 4, 1, tzinfo=UTC)
    )


def test_holdout_results_never_override_failed_acceptance_gates():
    artifact = train_artifact(_history(), sport="NBA", source_sha256="a" * 64)
    assert artifact["acceptance_gates"]["independent_source_verified"] is False
    assert artifact["acceptance_gates"]["source_authorization_documented"] is False
    assert artifact["acceptance_gates"]["model_owner_approval_documented"] is False
    assert artifact["certified"] is False
    with pytest.raises(ValueError, match="did not pass"):
        verify_artifact(
            artifact, sport="NBA", expected_sha256=artifact["artifact_sha256"]
        )


def test_artifact_tampering_and_expiry_are_rejected():
    artifact = train_artifact(_history(), sport="NBA", source_sha256="a" * 64)
    tampered = dict(artifact)
    tampered["elo"] = {"home_advantage": 999.0, "k": 999.0}
    with pytest.raises(ValueError, match="integrity"):
        verify_artifact(tampered, sport="NBA", require_certified=False)
    with pytest.raises(ValueError, match="stale"):
        verify_artifact(
            artifact,
            sport="NBA",
            require_certified=False,
            now=datetime(2027, 5, 1, tzinfo=UTC),
        )


def test_certified_artifact_requires_matching_runtime_sha_pin():
    artifact = train_artifact(
        _history(),
        sport="NBA",
        source_sha256="a" * 64,
        independent_source_sha256="b" * 64,
        source_authorization_reference="review://data-approval",
    )
    # Synthetic history may or may not satisfy model quality checks; validation
    # here tests only that an artifact cannot be loaded without its exact pin.
    with pytest.raises(ValueError, match="not pinned"):
        verify_artifact(artifact, sport="NBA", expected_sha256="c" * 64)


def test_runtime_history_must_match_the_artifact_provenance():
    games = _history()
    artifact = train_artifact(games, sport="NBA", source_sha256="a" * 64)
    verify_history_snapshot(games, artifact, sport="NBA")
    with pytest.raises(ValueError, match="does not match"):
        verify_history_snapshot(games[:-1], artifact, sport="NBA")


@pytest.mark.parametrize(
    "sport,expected_games,certified",
    [
        ("NBA", 2460, False),
        ("NHL", 5248, False),
    ],
)
def test_real_history_candidate_artifacts_report_holdout_and_remain_blocked(
    sport, expected_games, certified
):
    path = (
        Path(__file__).resolve().parents[1]
        / "reports"
        / "nba_nhl_model_candidates"
        / f"{sport.lower()}_calibrated.json"
    )
    artifact = json.loads(path.read_text())
    assert artifact["source_game_count"] == expected_games
    assert artifact["source_seasons"] == (
        [2024, 2025] if sport == "NBA" else [2022, 2023, 2024, 2025]
    )
    assert artifact["splits"]["same_day_isolated"] is True
    assert artifact["holdout_metrics"]["n_games"] >= 300
    assert artifact["certified"] is certified
    verify_artifact(artifact, sport=sport, require_certified=False)
