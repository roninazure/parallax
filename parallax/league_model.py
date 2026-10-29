"""Deterministic NBA/NHL Elo fitting, calibration, and artifact verification.

This module is independent of venue clients and shadow research code. Artifacts
are reproducible JSON with an embedded SHA-256 and explicit holdout gates.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .league_elo_experimental import BASE_RATING, FinalGame, _prepare

ARTIFACT_SCHEMA = 1
MODEL_VERSION = "parallax-league-elo-calibrated-v1"
MAX_ARTIFACT_AGE_DAYS = 370
BASELINE_PROBABILITY = 0.5
_ET = ZoneInfo("America/New_York")
PARAM_GRID = tuple(
    (home, k)
    for home in (0.0, 20.0, 40.0, 60.0, 80.0, 100.0)
    for k in (5.0, 10.0, 15.0, 20.0, 30.0, 40.0)
)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def history_fingerprint(games: Iterable[FinalGame], *, sport: str) -> str:
    rows = [g for g in _prepare(games) if g.sport == sport]
    encoded = {
        "sport": sport,
        "games": [
            [
                g.game_id,
                g.season,
                g.game_date.isoformat(),
                g.home_team,
                g.away_team,
                g.winner_team,
            ]
            for g in rows
        ],
    }
    return hashlib.sha256(_canonical(encoded)).hexdigest()


def verify_history_snapshot(
    games: Iterable[FinalGame], artifact: Mapping[str, Any], *, sport: str
) -> None:
    rows = [g for g in _prepare(games) if g.sport == sport]
    if (
        len(rows) != artifact.get("source_game_count")
        or sorted({g.season for g in rows}) != artifact.get("source_seasons")
        or history_fingerprint(rows, sport=sport) != artifact.get("history_fingerprint")
    ):
        raise ValueError(
            "runtime final-results history does not match the certified model source"
        )


def _logit(p: float) -> float:
    p = min(1 - 1e-12, max(1e-12, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-min(x, 700.0))
        return 1 / (1 + z)
    z = math.exp(max(x, -700.0))
    return z / (1 + z)


def _replay(rows: list[FinalGame], home_advantage: float, k: float):
    ratings: dict[tuple[str, str], float] = {}
    predicted: dict[tuple[str, str], float] = {}
    i = 0
    while i < len(rows):
        j = i + 1
        while j < len(rows) and rows[j].game_date == rows[i].game_date:
            j += 1
        day = rows[i:j]
        pending = []
        for game in day:
            h = ratings.get((game.sport, game.home_team), BASE_RATING)
            a = ratings.get((game.sport, game.away_team), BASE_RATING)
            p = 1 / (1 + 10 ** (-((h + home_advantage - a) / 400)))
            predicted[(game.sport, game.game_id)] = p
            pending.append((game, p))
        changes: dict[tuple[str, str], float] = {}
        for game, p in pending:
            delta = k * (float(game.winner_team == game.home_team) - p)
            hk, ak = (game.sport, game.home_team), (game.sport, game.away_team)
            changes[hk] = changes.get(hk, 0.0) + delta
            changes[ak] = changes.get(ak, 0.0) - delta
        for key, delta in changes.items():
            ratings[key] = ratings.get(key, BASE_RATING) + delta
        i = j
    return predicted, ratings


def _loss(ps: list[float], ys: list[int]) -> float:
    return -sum(
        y * math.log(max(1e-12, p)) + (1 - y) * math.log(max(1e-12, 1 - p))
        for p, y in zip(ps, ys, strict=True)
    ) / len(ys)


def _fit_platt(ps: list[float], ys: list[int]) -> tuple[float, float]:
    """Newton fit on calibration data only; deterministic and dependency-free."""
    if len(set(ys)) != 2:
        raise ValueError("calibration period must contain both outcomes")
    xs = [_logit(p) for p in ps]
    a, b = 0.0, 1.0
    for _ in range(100):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in zip(xs, ys, strict=True):
            p = _sigmoid(a + b * x)
            w = max(1e-9, p * (1 - p))
            d = p - y
            g0 += d
            g1 += d * x
            h00 += w
            h01 += w * x
            h11 += w * x * x
        det = h00 * h11 - h01 * h01
        if det <= 1e-12:
            break
        da = (h11 * g0 - h01 * g1) / det
        db = (h00 * g1 - h01 * g0) / det
        a -= da
        b -= db
        if max(abs(da), abs(db)) < 1e-8:
            break
    if not all(math.isfinite(x) for x in (a, b)) or b <= 0:
        raise ValueError("calibration fit is invalid or reverses forecast ranking")
    return a, b


def _fit_isotonic(ps: list[float], ys: list[int]) -> list[list[float]]:
    """Pooled-adjacent-violators calibration, with duplicate-x aggregation."""
    buckets: dict[float, list[int]] = {}
    for p, y in zip(ps, ys, strict=True):
        buckets.setdefault(p, []).append(y)
    blocks: list[list[float]] = []
    for x, outcomes in sorted(buckets.items()):
        blocks.append([x, x, float(len(outcomes)), float(sum(outcomes))])
        while (
            len(blocks) > 1
            and blocks[-2][3] / blocks[-2][2] > blocks[-1][3] / blocks[-1][2]
        ):
            right = blocks.pop()
            left = blocks.pop()
            blocks.append([left[0], right[1], left[2] + right[2], left[3] + right[3]])
    return [
        [(left + right) / 2, total / weight] for left, right, weight, total in blocks
    ]


def _apply_calibration(cal: Mapping[str, Any], p: float) -> float:
    if cal["method"] == "platt_logistic":
        value = _sigmoid(float(cal["intercept"]) + float(cal["slope"]) * _logit(p))
        return min(1 - 1e-9, max(1e-9, value))
    knots = cal.get("knots")
    if cal["method"] != "isotonic" or not isinstance(knots, list) or not knots:
        raise ValueError("unsupported calibration method")
    if p <= knots[0][0]:
        return min(1 - 1e-9, max(1e-9, float(knots[0][1])))
    if p >= knots[-1][0]:
        return min(1 - 1e-9, max(1e-9, float(knots[-1][1])))
    for (x0, y0), (x1, y1) in pairwise(knots):
        if x0 <= p <= x1:
            if x1 == x0:
                return min(1 - 1e-9, max(1e-9, float(y1)))
            value = float(y0) + (float(y1) - float(y0)) * (p - x0) / (x1 - x0)
            return min(1 - 1e-9, max(1e-9, value))
    return min(1 - 1e-9, max(1e-9, float(knots[-1][1])))


def _fit_calibrator(ps: list[float], ys: list[int]) -> dict[str, Any]:
    # Select calibration family using only an ordered split inside the
    # calibration period; the final holdout is never consulted here.
    if len(ps) < 2 or len(set(ys)) != 2:
        raise ValueError("calibration period needs both outcomes and a validation row")
    cut = min(len(ps) - 1, max(1, int(len(ps) * 0.8)))
    fit_ps, fit_ys = ps[:cut], ys[:cut]
    val_ps, val_ys = ps[cut:], ys[cut:]
    candidates = []
    try:
        a, b = _fit_platt(fit_ps, fit_ys)
        candidates.append(
            (
                _loss([_sigmoid(a + b * _logit(p)) for p in val_ps], val_ys),
                "platt_logistic",
            )
        )
    except ValueError:
        pass
    if val_ps:
        knots = _fit_isotonic(fit_ps, fit_ys)
        candidates.append(
            (
                _loss(
                    [
                        _apply_calibration({"method": "isotonic", "knots": knots}, p)
                        for p in val_ps
                    ],
                    val_ys,
                ),
                "isotonic",
            )
        )
    if not candidates:
        raise ValueError("no calibration method passed its training checks")
    method = min(candidates)[1]
    if method == "platt_logistic":
        a, b = _fit_platt(ps, ys)
        return {"method": method, "intercept": a, "slope": b}
    return {"method": method, "knots": _fit_isotonic(ps, ys)}


def _metrics(ps: list[float], ys: list[int]) -> dict[str, float | int]:
    n = len(ys)
    brier = sum((p - y) ** 2 for p, y in zip(ps, ys, strict=True)) / n
    ece = 0.0
    for bucket in range(10):
        pairs = [
            (p, y) for p, y in zip(ps, ys, strict=True) if min(9, int(p * 10)) == bucket
        ]
        if pairs:
            ece += (
                len(pairs)
                / n
                * abs(
                    sum(y for _, y in pairs) / len(pairs)
                    - sum(p for p, _ in pairs) / len(pairs)
                )
            )
    return {"n_games": n, "brier": brier, "log_loss": _loss(ps, ys), "ece_10_bins": ece}


def train_artifact(
    games: Iterable[FinalGame],
    *,
    sport: str,
    source_sha256: str,
    generated_at: datetime | None = None,
    independent_source_sha256: str | None = None,
    source_authorization_reference: str | None = None,
    model_owner_approval_reference: str | None = None,
) -> dict[str, Any]:
    rows = [g for g in _prepare(games) if g.sport == sport]
    if not rows or not re_full_sha(source_sha256):
        raise ValueError("model requires league finals and source SHA-256")
    dates = sorted({g.game_date for g in rows})
    n_train, n_cal = int(len(dates) * 0.60), int(len(dates) * 0.20)
    if n_train < 20 or n_cal < 10 or len(dates) - n_train - n_cal < 10:
        raise ValueError("insufficient chronological train/calibration/holdout dates")
    train_end, cal_end = dates[n_train - 1], dates[n_train + n_cal - 1]
    train = [g for g in rows if g.game_date <= train_end]
    calibration = [g for g in rows if train_end < g.game_date <= cal_end]
    holdout = [g for g in rows if g.game_date > cal_end]
    best = None
    for home_adv, k in PARAM_GRID:
        pred, _ = _replay(train, home_adv, k)
        ps = [pred[(sport, g.game_id)] for g in train]
        ys = [int(g.winner_team == g.home_team) for g in train]
        candidate = (_loss(ps, ys), home_adv, k)
        if best is None or candidate < best:
            best = candidate
    assert best is not None
    _, home_adv, k = best
    all_pred, ratings = _replay(rows, home_adv, k)
    cp = [all_pred[(sport, g.game_id)] for g in calibration]
    cy = [int(g.winner_team == g.home_team) for g in calibration]
    calibration_model = _fit_calibrator(cp, cy)
    hp = [
        _apply_calibration(calibration_model, all_pred[(sport, g.game_id)])
        for g in holdout
    ]
    hy = [int(g.winner_team == g.home_team) for g in holdout]
    metrics = _metrics(hp, hy)
    baseline_ps = [BASELINE_PROBABILITY] * len(holdout)
    baseline = _metrics(baseline_ps, hy)
    gates = {
        "holdout_nonempty": bool(holdout),
        "beats_half_brier": metrics["brier"] < baseline["brier"],
        "beats_half_log_loss": metrics["log_loss"] < baseline["log_loss"],
        "beats_half_ece": metrics["ece_10_bins"] < baseline["ece_10_bins"],
        "independent_source_verified": re_full_sha(independent_source_sha256 or "")
        and independent_source_sha256 != source_sha256,
        "source_authorization_documented": bool(
            source_authorization_reference and source_authorization_reference.strip()
        ),
        "model_owner_approval_documented": bool(
            model_owner_approval_reference and model_owner_approval_reference.strip()
        ),
    }
    artifact: dict[str, Any] = {
        "schema_version": ARTIFACT_SCHEMA,
        "model_version": MODEL_VERSION,
        "sport": sport,
        "generated_at": (
            generated_at
            or datetime.combine(rows[-1].game_date, datetime.min.time(), tzinfo=UTC)
        ).isoformat(),
        "trained_through": rows[-1].game_date.isoformat(),
        "source_sha256": source_sha256,
        "independent_source_sha256": independent_source_sha256,
        "source_authorization_reference": source_authorization_reference,
        "model_owner_approval_reference": model_owner_approval_reference,
        "source_game_count": len(rows),
        "source_seasons": sorted({g.season for g in rows}),
        "history_fingerprint": history_fingerprint(rows, sport=sport),
        "splits": {
            "train_end": train_end.isoformat(),
            "calibration_start": calibration[0].game_date.isoformat(),
            "calibration_end": cal_end.isoformat(),
            "holdout_start": holdout[0].game_date.isoformat(),
            "holdout_end": holdout[-1].game_date.isoformat(),
            "train_games": len(train),
            "calibration_games": len(calibration),
            "holdout_games": len(holdout),
            "same_day_isolated": True,
        },
        "elo": {"home_advantage": home_adv, "k": k},
        "calibration": calibration_model,
        "holdout_metrics": metrics,
        "baseline_50_50": baseline,
        "acceptance_gates": gates,
        "certified": all(gates.values()),
        "ratings": {
            f"{league}:{team}": rating
            for (league, team), rating in sorted(ratings.items())
            if league == sport
        },
    }
    artifact["artifact_sha256"] = hashlib.sha256(_canonical(artifact)).hexdigest()
    return artifact


def re_full_sha(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


@dataclass(frozen=True)
class CalibratedForecast:
    sport: str
    game_id: str
    game_date: date
    home_team: str
    away_team: str
    home_win_probability: float
    artifact_sha256: str
    model_version: str
    validation_reference: str


def verify_artifact(
    artifact: Mapping[str, Any],
    *,
    sport: str,
    now: datetime | None = None,
    require_certified: bool = True,
    expected_sha256: str | None = None,
) -> str:
    value = dict(artifact)
    digest = value.pop("artifact_sha256", None)
    actual = hashlib.sha256(_canonical(value)).hexdigest()
    if digest != actual or value.get("schema_version") != ARTIFACT_SCHEMA:
        raise ValueError("model artifact integrity/schema check failed")
    if require_certified and (
        not re_full_sha(expected_sha256 or "") or digest != expected_sha256
    ):
        raise ValueError(
            "model artifact SHA-256 is not pinned by runtime configuration"
        )
    if value.get("sport") != sport or value.get("model_version") != MODEL_VERSION:
        raise ValueError("model artifact league/version mismatch")
    if value.get("test_only") is True and "pytest" not in sys.modules:
        raise ValueError("test-only model artifact rejected")
    required_gates = {
        "holdout_nonempty",
        "beats_half_brier",
        "beats_half_log_loss",
        "beats_half_ece",
        "independent_source_verified",
        "source_authorization_documented",
        "model_owner_approval_documented",
    }
    gates = value.get("acceptance_gates")
    if (
        not isinstance(gates, dict)
        or set(gates) != required_gates
        or any(type(gate) is not bool for gate in gates.values())
    ):
        raise ValueError("model artifact acceptance gates are malformed")
    metrics = value.get("holdout_metrics", {})
    baseline = value.get("baseline_50_50", {})
    if not isinstance(metrics, dict) or not isinstance(baseline, dict):
        raise ValueError("model artifact metrics are malformed")  # noqa: TRY004
    derived = {
        "holdout_nonempty": type(metrics.get("n_games")) is int
        and metrics["n_games"] > 0,
        "beats_half_brier": _finite(metrics.get("brier"))
        and _finite(baseline.get("brier"))
        and metrics["brier"] < baseline["brier"],
        "beats_half_log_loss": _finite(metrics.get("log_loss"))
        and _finite(baseline.get("log_loss"))
        and metrics["log_loss"] < baseline["log_loss"],
        "beats_half_ece": _finite(metrics.get("ece_10_bins"))
        and _finite(baseline.get("ece_10_bins"))
        and metrics["ece_10_bins"] < baseline["ece_10_bins"],
        "independent_source_verified": re_full_sha(
            str(value.get("independent_source_sha256") or "")
        )
        and value.get("independent_source_sha256") != value.get("source_sha256"),
        "source_authorization_documented": isinstance(
            value.get("source_authorization_reference"), str
        )
        and bool(value["source_authorization_reference"].strip()),
        "model_owner_approval_documented": isinstance(
            value.get("model_owner_approval_reference"), str
        )
        and bool(value["model_owner_approval_reference"].strip()),
    }
    if gates != derived or value.get("certified") is not all(derived.values()):
        raise ValueError("model artifact gates do not match their evidence")
    if require_certified and (
        value.get("certified") is not True or not all(gates.values())
    ):
        raise ValueError("model artifact did not pass certification gates")
    splits = value.get("splits")
    if not isinstance(splits, dict) or splits.get("same_day_isolated") is not True:
        raise ValueError("model artifact does not prove same-day leakage isolation")
    calibration = value.get("calibration")
    if not isinstance(calibration, dict):
        raise ValueError("model artifact calibration is malformed")  # noqa: TRY004
    if calibration.get("method") == "platt_logistic":
        if not _finite(calibration.get("intercept")) or not _finite(calibration.get("slope")) or calibration["slope"] <= 0:
            raise ValueError("model artifact Platt parameters are invalid")
    elif calibration.get("method") == "isotonic":
        knots = calibration.get("knots")
        if not isinstance(knots, list) or not knots:
            raise ValueError("model artifact isotonic knots are malformed")
        previous_x = previous_y = -1.0
        for knot in knots:
            if (not isinstance(knot, list) or len(knot) != 2
                    or not _finite(knot[0]) or not _finite(knot[1])
                    or not 0 <= knot[0] <= 1 or not 0 <= knot[1] <= 1
                    or knot[0] <= previous_x or knot[1] < previous_y):
                raise ValueError("model artifact isotonic knots are invalid")
            previous_x, previous_y = knot
    else:
        raise ValueError("model artifact calibration method is unsupported")
    elo = value.get("elo")
    if (not isinstance(elo, dict) or not _finite(elo.get("home_advantage"))
            or not _finite(elo.get("k")) or elo["k"] <= 0):
        raise ValueError("model artifact Elo parameters are invalid")
    generated = datetime.fromisoformat(str(value.get("generated_at", "")))
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("model artifact has no timezone-aware generation time")
    now = now or datetime.now(UTC)
    if generated > now or now - generated > timedelta(days=MAX_ARTIFACT_AGE_DAYS):
        raise ValueError("model artifact is stale or future-dated")
    if not re_full_sha(str(value.get("source_sha256", ""))):
        raise ValueError("model artifact source provenance is invalid")
    if not re_full_sha(str(value.get("history_fingerprint", ""))):
        raise ValueError("model artifact normalized history fingerprint is invalid")
    return str(digest)


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def load_artifact(
    path: str | Path,
    *,
    sport: str,
    now: datetime | None = None,
    require_certified: bool = True,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("model artifact must be an object")  # noqa: TRY004
    verify_artifact(
        value,
        sport=sport,
        now=now,
        require_certified=require_certified,
        expected_sha256=expected_sha256,
    )
    return value


def predict(
    history: Iterable[FinalGame],
    artifact: Mapping[str, Any],
    *,
    sport: str,
    game_id: str,
    game_date: date,
    home_team: str,
    away_team: str,
    now: datetime | None = None,
    expected_sha256: str | None = None,
) -> CalibratedForecast:
    digest = verify_artifact(
        artifact, sport=sport, now=now, expected_sha256=expected_sha256
    )
    history_rows = list(history)
    verify_history_snapshot(history_rows, artifact, sport=sport)
    if game_date <= date.fromisoformat(artifact["trained_through"]):
        raise ValueError("fixture date is not after the artifact training cutoff")
    rows = [
        g
        for g in _prepare(history_rows)
        if g.sport == sport and g.game_date < game_date
    ]
    hp = float(artifact["elo"]["home_advantage"])
    k = float(artifact["elo"]["k"])
    ratings: dict[tuple[str, str], float] = {}
    pred, _ = _replay(rows, hp, k)
    # Apply chronological frozen predictions to update team ratings.
    i = 0
    while i < len(rows):
        j = i + 1
        while j < len(rows) and rows[j].game_date == rows[i].game_date:
            j += 1
        changes: dict[tuple[str, str], float] = {}
        for game in rows[i:j]:
            p = pred[(sport, game.game_id)]
            delta = k * (float(game.winner_team == game.home_team) - p)
            hk, ak = (sport, game.home_team), (sport, game.away_team)
            changes[hk] = changes.get(hk, 0.0) + delta
            changes[ak] = changes.get(ak, 0.0) - delta
        for key, delta in changes.items():
            ratings[key] = ratings.get(key, BASE_RATING) + delta
        i = j
    raw = 1 / (
        1
        + 10
        ** (
            -(
                (
                    ratings.get((sport, home_team), BASE_RATING)
                    + hp
                    - ratings.get((sport, away_team), BASE_RATING)
                )
                / 400
            )
        )
    )
    p = _apply_calibration(artifact["calibration"], raw)
    return CalibratedForecast(
        sport,
        game_id,
        game_date,
        home_team,
        away_team,
        p,
        digest,
        str(artifact["model_version"]),
        f"sha256:{digest}",
    )


def build_evidence(
    market,
    fixture,
    forecast: CalibratedForecast,
    *,
    now: datetime,
    selected_yes_team: str,
    artifact: Mapping[str, Any],
):
    """Bind a verified calibrated forecast to one exact mapped contract."""
    from .models import Evidence
    from .normalization import rules_digest

    try:
        verify_artifact(
            artifact,
            sport=forecast.sport,
            now=now,
            expected_sha256=forecast.artifact_sha256,
        )
    except (TypeError, ValueError):
        return None
    digest = str(forecast.artifact_sha256)
    if (
        now.tzinfo is None
        or now.utcoffset() is None
        or now >= fixture.kickoff
        or fixture.sport != forecast.sport
        or fixture.game_id != forecast.game_id
        or fixture.home_team != forecast.home_team
        or fixture.away_team != forecast.away_team
        or fixture.kickoff.astimezone(_ET).date() != forecast.game_date
        or forecast.model_version != artifact.get("model_version")
        or forecast.validation_reference != f"sha256:{digest}"
        or not _finite(forecast.home_win_probability)
        or not 0 < forecast.home_win_probability < 1
        or market.status != "OPEN"
        or not market.resolution_rules.strip()
        or selected_yes_team not in {fixture.home_team, fixture.away_team}
    ):
        return None
    yes_p = (
        forecast.home_win_probability
        if selected_yes_team == fixture.home_team
        else 1 - forecast.home_win_probability
    )
    return Evidence(
        venue=market.venue,
        market_id=market.venue_market_id,
        fair_probability=yes_p,
        source="PARALLAX_CALIBRATED_LEAGUE_MODEL",
        model_version=forecast.model_version,
        observed_at=now.isoformat(),
        valid_until=min(now + timedelta(minutes=15), fixture.kickoff).isoformat(),
        rules_digest=rules_digest(market),
        review_reference=forecast.validation_reference,
        rationale="Chronologically calibrated league forecast bound to verified full-game winner contract.",
        independent_sources=(
            f"primary_results:{artifact['source_sha256']}",
            f"independent_results:{artifact['independent_source_sha256']}",
        ),
        validation_reference=forecast.validation_reference,
        validation_status="CALIBRATED",
        source_independence="independent historical results sources",
        forecast_metadata={
            "sport": forecast.sport,
            "game_id": forecast.game_id,
            "model_artifact_sha256": forecast.artifact_sha256,
        },
    )
