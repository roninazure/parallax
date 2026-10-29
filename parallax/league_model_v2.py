"""Feature-based, deterministic NBA/NHL probability model (exploratory v2).

Features are generated strictly before each ET game date from final winners:
decayed overall and venue form, opponent-adjusted form, rest, and season
boundaries. The module has no network or venue dependencies.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from .league_elo_experimental import FinalGame, _prepare

MODEL_VERSION_V2 = "parallax-league-logistic-form-v2"
SCHEMA_V2 = 2
FEATURE_NAMES = (
    "intercept",
    "overall_form_diff",
    "home_split_form",
    "away_split_form",
    "opponent_adjusted_form_diff",
    "rest_days_diff",
)
DECAY = 0.88


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _history_fingerprint(rows: Iterable[FinalGame], sport: str) -> str:
    chosen = [g for g in _prepare(rows) if g.sport == sport]
    value = {
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
            for g in chosen
        ],
    }
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-min(value, 700.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(value, -700.0))
    return z / (1.0 + z)


def _features(
    rows: Iterable[FinalGame], *, sport: str
) -> dict[tuple[str, str], list[float]]:
    games = [g for g in _prepare(rows) if g.sport == sport]
    state: dict[str, dict[str, Any]] = {}
    output: dict[tuple[str, str], list[float]] = {}
    i = 0
    while i < len(games):
        j = i + 1
        while j < len(games) and games[j].game_date == games[i].game_date:
            j += 1
        day = games[i:j]

        def team_state(team: str, season: int) -> dict[str, Any]:
            old = state.get(team)
            if old is None or old["season"] != season:
                return {
                    "season": season,
                    "overall": 0.0,
                    "home": 0.0,
                    "away": 0.0,
                    "adj": 0.0,
                    "last": None,
                }
            return old

        prepared = []
        for game in day:
            h, a = (
                team_state(game.home_team, game.season),
                team_state(game.away_team, game.season),
            )
            hrest = (
                7 if h["last"] is None else min(10, (game.game_date - h["last"]).days)
            )
            arest = (
                7 if a["last"] is None else min(10, (game.game_date - a["last"]).days)
            )
            x = [
                1.0,
                h["overall"] - a["overall"],
                h["home"],
                -a["away"],
                h["adj"] - a["adj"],
                (hrest - arest) / 10.0,
            ]
            output[(sport, game.game_id)] = x
            prepared.append((game, h, a))
        updates: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for game, h, a in prepared:
            home_win = float(game.winner_team == game.home_team)
            # Opponent-strength adjustment uses the pre-date form state.
            expected = _sigmoid((h["overall"] - a["overall"]) * 2.0 + 0.12)
            residual = home_win - expected
            updates[game.home_team].append(("overall", home_win - 0.5))
            updates[game.home_team].append(("home", home_win - 0.5))
            updates[game.home_team].append(("adj", residual))
            updates[game.away_team].append(("overall", (1.0 - home_win) - 0.5))
            updates[game.away_team].append(("away", (1.0 - home_win) - 0.5))
            updates[game.away_team].append(("adj", -residual))
        for game, _, _ in prepared:
            for team in (game.home_team, game.away_team):
                current = team_state(team, game.season)
                state[team] = current
            h, a = state[game.home_team], state[game.away_team]
            h["last"] = a["last"] = game.game_date
        for team, values in updates.items():
            current = state[team]
            grouped: dict[str, list[float]] = defaultdict(list)
            for name, value in values:
                grouped[name].append(value)
            for name, val in grouped.items():
                current[name] = DECAY * current[name] + (1 - DECAY) * sum(val) / len(
                    val
                )
        i = j
    return output


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    n = len(rhs)
    a = [matrix[i][:] + [rhs[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(a[row][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("singular logistic Hessian")
        a[col], a[pivot] = a[pivot], a[col]
        scale = a[col][col]
        a[col] = [v / scale for v in a[col]]
        for row in range(n):
            if row == col:
                continue
            factor = a[row][col]
            a[row] = [v - factor * w for v, w in zip(a[row], a[col], strict=True)]
    return [a[i][-1] for i in range(n)]


def _fit_logistic(xs: list[list[float]], ys: list[int], l2: float) -> list[float]:
    if not xs or len(xs) != len(ys) or len(set(ys)) < 2:
        raise ValueError("logistic training requires both classes")
    if not xs[0] or any(len(x) != len(xs[0]) for x in xs):
        raise ValueError("logistic feature vectors have inconsistent dimensions")
    beta = [0.0] * len(xs[0])
    for _ in range(100):
        grad = [0.0] * len(beta)
        hess = [[0.0] * len(beta) for _ in beta]
        for x, y in zip(xs, ys, strict=True):
            p = _sigmoid(sum(b * v for b, v in zip(beta, x, strict=True)))
            w = max(1e-8, p * (1 - p))
            for r in range(len(beta)):
                grad[r] += x[r] * (y - p)
                for c in range(len(beta)):
                    hess[r][c] += w * x[r] * x[c]
        for k in range(1, len(beta)):
            grad[k] -= l2 * beta[k]
            hess[k][k] += l2
        step = _solve(hess, grad)
        beta = [b + d for b, d in zip(beta, step, strict=True)]
        if max(abs(d) for d in step) < 1e-8:
            break
    if not all(math.isfinite(v) for v in beta):
        raise ValueError("non-finite logistic coefficient")
    return beta


def _loss(ps: list[float], ys: list[int]) -> float:
    return -sum(
        y * math.log(max(1e-12, p)) + (1 - y) * math.log(max(1e-12, 1 - p))
        for p, y in zip(ps, ys, strict=True)
    ) / len(ys)


def _fit_platt(ps: list[float], ys: list[int]) -> dict[str, float]:
    # Regularized Newton fit on the disjoint calibration window.
    xs = [
        [
            1.0,
            math.log(
                min(1 - 1e-12, max(1e-12, p)) / (1 - min(1 - 1e-12, max(1e-12, p)))
            ),
        ]
        for p in ps
    ]
    beta = _fit_logistic(xs, ys, 1e-5)
    return {"intercept": beta[0], "slope": beta[1]}


def _metrics(ps: list[float], ys: list[int]) -> dict[str, float | int]:
    n = len(ys)
    brier = sum((p - y) ** 2 for p, y in zip(ps, ys, strict=True)) / n
    ece = 0.0
    bins = []
    for b in range(10):
        sample = [
            (p, y) for p, y in zip(ps, ys, strict=True) if min(9, int(p * 10)) == b
        ]
        if sample:
            conf = sum(p for p, _ in sample) / len(sample)
            rate = sum(y for _, y in sample) / len(sample)
            ece += len(sample) / n * abs(conf - rate)
            bins.append(
                {
                    "bin": b,
                    "n": len(sample),
                    "mean_probability": conf,
                    "home_win_rate": rate,
                }
            )
    return {
        "n_games": n,
        "brier": brier,
        "log_loss": _loss(ps, ys),
        "ece_10_bins": ece,
        "reliability_bins": bins,
    }


def _block_intervals(
    ps: list[float], ys: list[int], dates: list[date]
) -> dict[str, Any]:
    blocks: dict[date, list[int]] = defaultdict(list)
    for i, day in enumerate(dates):
        blocks[day].append(i)
    days = sorted(blocks)
    if len(days) < 2:
        return {
            "method": "ET-date-block-bootstrap",
            "n_blocks": len(days),
            "replicates": 0,
        }
    rng = random.Random(20261029)
    samples = {name: [] for name in ("brier", "log_loss", "ece_10_bins")}
    for _ in range(400):
        indices = [
            i
            for day in (days[rng.randrange(len(days))] for _ in days)
            for i in blocks[day]
        ]
        metrics = _metrics([ps[i] for i in indices], [ys[i] for i in indices])
        for name, values in samples.items():
            values.append(float(metrics[name]))
    return {
        "method": "ET-date-block-bootstrap",
        "n_blocks": len(days),
        "replicates": 400,
        "confidence_level": 0.95,
        "intervals": {
            name: [sorted(vals)[9], sorted(vals)[389]] for name, vals in samples.items()
        },
    }


def train_artifact_v2(
    games: Iterable[FinalGame],
    *,
    sport: str,
    source_sha256: str,
    generated_at: datetime | None = None,
    forward_holdout: Iterable[FinalGame] | None = None,
    forward_holdout_source_sha256: str | None = None,
    independent_source_sha256: str | None = None,
    independent_games: Iterable[FinalGame] | None = None,
    source_authorization_reference: str | None = None,
    model_owner_approval_reference: str | None = None,
) -> dict[str, Any]:
    rows = [g for g in _prepare(games) if g.sport == sport]
    if sport not in {"NBA", "NHL"} or not rows or not _full_sha(source_sha256):
        raise ValueError("v2 requires NBA/NHL finals and a source SHA-256")
    if any(
        team != team.strip().upper()
        for g in rows
        for team in (g.home_team, g.away_team)
    ):
        raise ValueError("v2 team identities must be canonical uppercase codes")
    independent_fingerprint = None
    if independent_games is not None:
        independent_rows = [g for g in _prepare(independent_games) if g.sport == sport]
        independent_fingerprint = _history_fingerprint(independent_rows, sport)
        if independent_fingerprint != _history_fingerprint(rows, sport):
            raise ValueError(
                "independent source results do not reconcile game-for-game"
            )
    dates = sorted({g.game_date for g in rows})
    n_train, n_cal = int(0.60 * len(dates)), int(0.20 * len(dates))
    if n_train < 20 or n_cal < 10 or len(dates) - n_train - n_cal < 10:
        raise ValueError("insufficient chronological v2 split dates")
    train_end, cal_end = dates[n_train - 1], dates[n_train + n_cal - 1]
    train = [g for g in rows if g.game_date <= train_end]
    calibration = [g for g in rows if train_end < g.game_date <= cal_end]
    historical_holdout = [g for g in rows if g.game_date > cal_end]
    external_holdout = [g for g in (forward_holdout or ()) if g.sport == sport]
    fresh = bool(external_holdout)
    if fresh:
        if not _full_sha(forward_holdout_source_sha256):
            raise ValueError("forward holdout requires its source SHA-256")
        if min(g.game_date for g in external_holdout) <= rows[-1].game_date:
            raise ValueError(
                "forward holdout must begin after all training snapshot dates"
            )
        if len({g.game_id for g in external_holdout}) != len(external_holdout):
            raise ValueError("forward holdout contains duplicate game IDs")
        holdout = external_holdout
        feature_rows = [*rows, *external_holdout]
    else:
        holdout = historical_holdout
        feature_rows = rows
    feat = _features(feature_rows, sport=sport)
    train_x = [feat[(sport, g.game_id)] for g in train]
    train_y = [int(g.winner_team == g.home_team) for g in train]
    split = max(1, int(0.8 * len(train)))
    selection = []
    for l2 in (0.01, 0.1, 1.0, 10.0):
        beta = _fit_logistic(train_x[:split], train_y[:split], l2)
        ps = [
            _sigmoid(sum(b * v for b, v in zip(beta, x, strict=True)))
            for x in train_x[split:]
        ]
        selection.append((_loss(ps, train_y[split:]), "logistic", l2))
    train_rate = (sum(train_y[:split]) + 1.0) / (len(train_y[:split]) + 2.0)
    selection.append(
        (
            _loss([train_rate] * (len(train_y) - split), train_y[split:]),
            "home_rate",
            0.0,
        )
    )
    selected_loss, prediction_family, chosen_l2 = min(selection)
    beta = (
        _fit_logistic(train_x, train_y, chosen_l2)
        if prediction_family == "logistic"
        else [0.0] * len(FEATURE_NAMES)
    )
    train_rate = (sum(train_y) + 1.0) / (len(train_y) + 2.0)
    cal_ps = (
        [
            _sigmoid(
                sum(b * v for b, v in zip(beta, feat[(sport, g.game_id)], strict=True))
            )
            for g in calibration
        ]
        if prediction_family == "logistic"
        else [train_rate] * len(calibration)
    )
    cal_y = [int(g.winner_team == g.home_team) for g in calibration]
    # Calibration is fitted only on its disjoint chronological window.
    cal_model = (
        {"method": "platt_logistic", **_fit_platt(cal_ps, cal_y)}
        if prediction_family == "logistic"
        else {
            "method": "home_rate",
            "probability": (sum(cal_y) + 1.0) / (len(cal_y) + 2.0),
        }
    )

    def calibrate(raw: float) -> float:
        if cal_model["method"] == "home_rate":
            return float(cal_model["probability"])
        logit = math.log(
            min(1 - 1e-12, max(1e-12, raw)) / (1 - min(1 - 1e-12, max(1e-12, raw)))
        )
        return _sigmoid(cal_model["intercept"] + cal_model["slope"] * logit)

    raw_hold = (
        [
            _sigmoid(
                sum(b * v for b, v in zip(beta, feat[(sport, g.game_id)], strict=True))
            )
            for g in holdout
        ]
        if prediction_family == "logistic"
        else [train_rate] * len(holdout)
    )
    hp = [calibrate(p) for p in raw_hold]
    hy = [int(g.winner_team == g.home_team) for g in holdout]
    metrics, baseline = _metrics(hp, hy), _metrics([0.5] * len(hy), hy)
    home_rate = (sum(train_y) + 1.0) / (len(train_y) + 2.0)
    home_rate_metrics = _metrics([home_rate] * len(hy), hy)
    # Like-for-like Elo comparison: choose Elo parameters on the same train
    # window, calibrate only on the disjoint calibration window, then score
    # the same holdout once.
    from .league_model import PARAM_GRID, _apply_calibration, _fit_calibrator, _replay

    best_elo = None
    for home_adv, k in PARAM_GRID:
        pred_train, _ = _replay(train, home_adv, k)
        loss = _loss([pred_train[(sport, g.game_id)] for g in train], train_y)
        candidate = (loss, home_adv, k)
        if best_elo is None or candidate < best_elo:
            best_elo = candidate
    assert best_elo is not None
    _, elo_home, elo_k = best_elo
    elo_pred, _ = _replay(
        [*rows, *external_holdout] if fresh else rows, elo_home, elo_k
    )
    elo_cal_ps = [elo_pred[(sport, g.game_id)] for g in calibration]
    elo_calibrator = _fit_calibrator(elo_cal_ps, cal_y)
    elo_raw_hold = [elo_pred[(sport, g.game_id)] for g in holdout]
    elo_hp = [_apply_calibration(elo_calibrator, p) for p in elo_raw_hold]
    elo_metrics = _metrics(elo_hp, hy)
    best_logistic_l2 = min(
        (loss, l2) for loss, family, l2 in selection if family == "logistic"
    )[1]
    logistic_beta = _fit_logistic(train_x, train_y, best_logistic_l2)
    logistic_cal_raw = [
        _sigmoid(
            sum(
                b * v
                for b, v in zip(logistic_beta, feat[(sport, g.game_id)], strict=True)
            )
        )
        for g in calibration
    ]
    logistic_calibrator = _fit_platt(logistic_cal_raw, cal_y)
    logistic_hold_raw = [
        _sigmoid(
            sum(
                b * v
                for b, v in zip(logistic_beta, feat[(sport, g.game_id)], strict=True)
            )
        )
        for g in holdout
    ]
    logistic_hp = []
    for p in logistic_hold_raw:
        logit = math.log(
            min(1 - 1e-12, max(1e-12, p)) / (1 - min(1 - 1e-12, max(1e-12, p)))
        )
        logistic_hp.append(
            _sigmoid(
                logistic_calibrator["intercept"] + logistic_calibrator["slope"] * logit
            )
        )
    logistic_metrics = _metrics(logistic_hp, hy)
    gates = {
        "holdout_nonempty": bool(holdout),
        "beats_half_brier": metrics["brier"] < baseline["brier"],
        "beats_half_log_loss": metrics["log_loss"] < baseline["log_loss"],
        "beats_half_ece": metrics["ece_10_bins"] < baseline["ece_10_bins"],
        "independent_source_verified": bool(
            independent_source_sha256
            and independent_source_sha256 != source_sha256
            and independent_fingerprint == _history_fingerprint(rows, sport)
        ),
        "source_authorization_documented": bool(
            source_authorization_reference and source_authorization_reference.strip()
        ),
        "model_owner_approval_documented": bool(
            model_owner_approval_reference and model_owner_approval_reference.strip()
        ),
        "fresh_forward_holdout": fresh,
    }
    raw_source = source_sha256.lower()
    artifact = {
        "schema_version": SCHEMA_V2,
        "model_version": MODEL_VERSION_V2,
        "sport": sport,
        "generated_at": (
            generated_at
            or datetime.combine(
                (holdout[-1] if fresh else rows[-1]).game_date,
                datetime.min.time(),
                tzinfo=UTC,
            )
        ).isoformat(),
        "trained_through": rows[-1].game_date.isoformat(),
        "source_sha256": raw_source,
        "independent_source_sha256": independent_source_sha256,
        "independent_history_fingerprint": independent_fingerprint,
        "source_authorization_reference": source_authorization_reference,
        "model_owner_approval_reference": model_owner_approval_reference,
        "forward_holdout_source_sha256": forward_holdout_source_sha256
        if fresh
        else None,
        "source_game_count": len(rows),
        "source_seasons": sorted({g.season for g in rows}),
        "teams": sorted({team for g in rows for team in (g.home_team, g.away_team)}),
        "history_fingerprint": _history_fingerprint(rows, sport),
        "features": list(FEATURE_NAMES),
        "feature_decay": DECAY,
        "selection": {
            "method": "chronological log-loss within train",
            "prediction_family": prediction_family,
            "validation_log_loss": selected_loss,
            "l2": chosen_l2,
            "candidate_validation_log_loss": [
                {"family": family, "l2": l2, "log_loss": loss}
                for loss, family, l2 in selection
            ],
        },
        "prediction_family": prediction_family,
        "coefficients": beta,
        "calibration": cal_model,
        "splits": {
            "train_end": train_end.isoformat(),
            "calibration_start": calibration[0].game_date.isoformat(),
            "calibration_end": cal_end.isoformat(),
            "holdout_start": holdout[0].game_date.isoformat(),
            "holdout_end": holdout[-1].game_date.isoformat(),
            "train_games": len(train),
            "calibration_games": len(calibration),
            "exploratory_previous_holdout_games": len(historical_holdout),
            "holdout_games": len(holdout),
            "same_day_isolated": True,
            "holdout_status": "FRESH_INDEPENDENT"
            if fresh
            else "EXPLORATORY_PREVIOUSLY_INSPECTED",
        },
        "holdout_metrics": metrics,
        "baseline_50_50": baseline,
        "model_comparison": {
            "selected_v2": metrics,
            "feature_logistic": {"l2": best_logistic_l2, **logistic_metrics},
            "elo_calibrated_same_splits": {
                "home_advantage": elo_home,
                "k": elo_k,
                **elo_metrics,
            },
            "constant_home_rate_fit_on_train": {
                "probability": home_rate,
                **home_rate_metrics,
            },
        },
        "holdout_confidence_intervals": _block_intervals(
            hp, hy, [g.game_date for g in holdout]
        ),
        "baseline_confidence_intervals": {
            "50_50": _block_intervals(
                [0.5] * len(hy), hy, [g.game_date for g in holdout]
            ),
            "constant_home_rate": _block_intervals(
                [home_rate] * len(hy), hy, [g.game_date for g in holdout]
            ),
            "elo_calibrated": _block_intervals(
                elo_hp, hy, [g.game_date for g in holdout]
            ),
            "feature_logistic": _block_intervals(
                logistic_hp, hy, [g.game_date for g in holdout]
            ),
        },
        "acceptance_gates": gates,
        "certified": all(gates.values()),
    }
    artifact["artifact_sha256"] = hashlib.sha256(_canonical(artifact)).hexdigest()
    return artifact


def verify_artifact_v2(
    artifact: Mapping[str, Any],
    *,
    sport: str,
    now: datetime,
    expected_sha256: str | None,
    require_certified: bool = True,
) -> str:
    value = dict(artifact)
    digest = value.pop("artifact_sha256", None)
    if (
        digest != hashlib.sha256(_canonical(value)).hexdigest()
        or value.get("schema_version") != SCHEMA_V2
    ):
        raise ValueError("v2 model artifact integrity/schema check failed")
    if value.get("model_version") != MODEL_VERSION_V2 or value.get("sport") != sport:
        raise ValueError("v2 model league/version mismatch")
    if value.get("test_only") is True and "pytest" not in sys.modules:
        raise ValueError("test-only v2 artifact rejected")
    gates = value.get("acceptance_gates")
    required = {
        "holdout_nonempty",
        "beats_half_brier",
        "beats_half_log_loss",
        "beats_half_ece",
        "independent_source_verified",
        "source_authorization_documented",
        "model_owner_approval_documented",
        "fresh_forward_holdout",
    }
    metrics, baseline = (
        value.get("holdout_metrics", {}),
        value.get("baseline_50_50", {}),
    )
    if not isinstance(metrics, dict) or not isinstance(baseline, dict):
        raise TypeError("v2 holdout metrics are malformed")
    if (
        not isinstance(gates, dict)
        or set(gates) != required
        or any(type(v) is not bool for v in gates.values())
    ):
        raise ValueError("v2 acceptance gates are malformed")
    splits = value.get("splits")
    if (
        not isinstance(splits, dict)
        or splits.get("same_day_isolated") is not True
        or type(splits.get("train_games")) is not int
        or splits["train_games"] <= 0
        or type(splits.get("calibration_games")) is not int
        or splits["calibration_games"] <= 0
        or splits.get("holdout_games") != metrics.get("n_games")
    ):
        raise ValueError("v2 chronological split manifest is malformed")

    try:
        train_end = date.fromisoformat(str(splits["train_end"]))
        calibration_start = date.fromisoformat(str(splits["calibration_start"]))
        calibration_end = date.fromisoformat(str(splits["calibration_end"]))
        holdout_start = date.fromisoformat(str(splits["holdout_start"]))
        holdout_end = date.fromisoformat(str(splits["holdout_end"]))
        trained_through = date.fromisoformat(str(value["trained_through"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("v2 split dates are malformed") from exc
    if (
        not train_end
        < calibration_start
        <= calibration_end
        < holdout_start
        <= holdout_end
    ):
        raise ValueError("v2 train, calibration, and holdout dates overlap or reverse")
    if (
        splits.get("holdout_status") == "FRESH_INDEPENDENT"
        and holdout_start <= trained_through
    ):
        raise ValueError("v2 forward holdout is not later than training data")

    def full_sha(raw: object) -> bool:
        return (
            isinstance(raw, str)
            and len(raw) == 64
            and all(c in "0123456789abcdef" for c in raw.lower())
        )

    derived = {
        "holdout_nonempty": type(metrics.get("n_games")) is int
        and metrics["n_games"] > 0,
        "beats_half_brier": _finite(metrics.get("brier"))
        and metrics["brier"] < baseline.get("brier", 0),
        "beats_half_log_loss": _finite(metrics.get("log_loss"))
        and metrics["log_loss"] < baseline.get("log_loss", 0),
        "beats_half_ece": _finite(metrics.get("ece_10_bins"))
        and metrics["ece_10_bins"] < baseline.get("ece_10_bins", 0),
        "independent_source_verified": full_sha(value.get("independent_source_sha256"))
        and value.get("independent_source_sha256") != value.get("source_sha256")
        and value.get("independent_history_fingerprint")
        == value.get("history_fingerprint"),
        "source_authorization_documented": isinstance(
            value.get("source_authorization_reference"), str
        )
        and bool(value["source_authorization_reference"].strip()),
        "model_owner_approval_documented": isinstance(
            value.get("model_owner_approval_reference"), str
        )
        and bool(value["model_owner_approval_reference"].strip()),
        "fresh_forward_holdout": splits.get("holdout_status") == "FRESH_INDEPENDENT",
    }
    if gates != derived or value.get("certified") is not all(derived.values()):
        raise ValueError("v2 gates do not match recorded evidence")
    if require_certified and (
        value.get("certified") is not True
        or digest != expected_sha256
        or not all(gates.values())
    ):
        raise ValueError("v2 artifact is exploratory, uncertified, or unpinned")
    if (
        not isinstance(value.get("coefficients"), list)
        or len(value["coefficients"]) != len(FEATURE_NAMES)
        or not all(_finite(x) for x in value["coefficients"])
    ):
        raise ValueError("v2 coefficients are malformed")
    cal = value.get("calibration", {})
    if value.get("prediction_family") == "logistic":
        if (
            cal.get("method") != "platt_logistic"
            or not _finite(cal.get("intercept"))
            or not _finite(cal.get("slope"))
            or cal["slope"] <= 0
        ):
            raise ValueError("v2 calibration is malformed")
    elif value.get("prediction_family") == "home_rate":
        if (
            cal.get("method") != "home_rate"
            or not _finite(cal.get("probability"))
            or not 0 < cal["probability"] < 1
        ):
            raise ValueError("v2 home-rate calibration is malformed")
    else:
        raise ValueError("v2 prediction family is unsupported")
    created = datetime.fromisoformat(str(value.get("generated_at", "")))
    if (
        created.tzinfo is None
        or created.utcoffset() is None
        or created > now
        or (now - created).days > 370
    ):
        raise ValueError("v2 model artifact is stale or future-dated")
    if (
        not isinstance(value.get("teams"), list)
        or not value["teams"]
        or len(set(value["teams"])) != len(value["teams"])
    ):
        raise ValueError("v2 team identities are malformed")
    if any(
        not isinstance(team, str) or not team or team != team.strip().upper()
        for team in value["teams"]
    ):
        raise ValueError("v2 team identities are not canonical")
    if (
        value.get("features") != list(FEATURE_NAMES)
        or value.get("feature_decay") != DECAY
    ):
        raise ValueError("v2 feature schema/decay is unsupported")
    if (
        type(value.get("source_game_count")) is not int
        or value["source_game_count"] <= 0
    ):
        raise ValueError("v2 source coverage is malformed")
    if not full_sha(value.get("history_fingerprint")):
        raise ValueError("v2 normalized history fingerprint is malformed")
    if not full_sha(value.get("source_sha256")):
        raise ValueError("v2 source provenance is malformed")
    if derived["fresh_forward_holdout"] and not full_sha(
        value.get("forward_holdout_source_sha256")
    ):
        raise ValueError("v2 forward holdout source provenance is malformed")
    return str(digest)


def _finite(value: object) -> bool:
    return type(value) in (float, int) and math.isfinite(value)


def _full_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value.lower())
    )


def predict_v2(features: list[float], artifact: Mapping[str, Any]) -> float:
    if len(features) != len(FEATURE_NAMES) or len(
        artifact.get("coefficients", [])
    ) != len(FEATURE_NAMES):
        raise ValueError("v2 feature vector shape mismatch")
    cal = artifact["calibration"]
    if artifact.get("prediction_family") == "home_rate":
        return float(cal["probability"])
    raw = _sigmoid(
        sum(a * b for a, b in zip(features, artifact["coefficients"], strict=True))
    )
    logit = math.log(
        min(1 - 1e-12, max(1e-12, raw)) / (1 - min(1 - 1e-12, max(1e-12, raw)))
    )
    return _sigmoid(float(cal["intercept"]) + float(cal["slope"]) * logit)


def forecast_features_v2(
    history: Iterable[FinalGame],
    *,
    sport: str,
    game_date: date,
    season: int,
    home_team: str,
    away_team: str,
    allowed_teams: Iterable[str],
) -> list[float]:
    if not home_team or not away_team or home_team == away_team:
        raise ValueError("invalid v2 forecast team identity")
    known = set(allowed_teams)
    if home_team not in known or away_team not in known:
        raise ValueError("v2 forecast team is absent from verified model history")
    rows = [g for g in _prepare(history) if g.sport == sport]
    if any(g.game_date >= game_date for g in rows):
        raise ValueError("v2 forecast history contains same-day or future results")
    target = FinalGame(
        sport, "__v2_forecast__", season, game_date, home_team, away_team, home_team
    )
    return _features([*rows, target], sport=sport)[(sport, target.game_id)]
