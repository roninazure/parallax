"""Strict offline NHL prospectively frozen forecast vs official final reconciliation.

No network, scheduling, publication, notification, or BUY authorization.
The archive is read as untrusted data. A legitimate pregame record and exact
NHL game identity are required before an outcome can be scored.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MAX_SNAPSHOTS = 1000
MIN_AFTER_START = timedelta(hours=4)
GAME_ID = re.compile(r"nhl:([0-9]{10})\Z")
TICKER = re.compile(r"KXNHLGAME-[A-Z0-9]{8,32}\Z")
TEAM = re.compile(r"[A-Z]{3}\Z")


class ReconciliationError(ValueError):
    """Untrusted or mismatched source data: fail closed."""


@dataclass(frozen=True)
class FrozenForecast:
    game_id: str
    matchup: str
    event_ticker: str
    start_utc: datetime
    forecast_captured_utc: datetime
    home_probability: float
    snapshot_name: str

    @property
    def away(self) -> str:
        return self.matchup.split("@")[0]

    @property
    def home(self) -> str:
        return self.matchup.split("@")[1]


def instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ReconciliationError("Timestamp missing")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError("naive timestamp")
        return dt.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise ReconciliationError("Invalid timestamp") from exc


def archived_forecasts(directory: Path, *, now: datetime) -> dict[str, FrozenForecast]:
    """Use earliest pregame probability per NHL game (no post-hoc selection).

    Read *all* bounded archives. If over capacity or malformed, fail closed.
    The original archive remains immutable; rejected contracts do not count.
    """
    if now.utcoffset() is None:
        raise ReconciliationError("Aware clock required")
    if not directory.exists():
        return {}
    paths = sorted(directory.glob("nhl-shadow-*.json"))
    if len(paths) > MAX_SNAPSHOTS:
        raise ReconciliationError("Too many unprocessed snapshots; inspect/rotate safely")
    forecasts: dict[str, FrozenForecast] = {}
    identity: dict[str, tuple[str, str, datetime]] = {}
    for path in paths:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(obj, dict) or obj.get("mode") != "RESEARCH_ONLY":
                raise ReconciliationError("Bad research snapshot mode")
            if any(obj.get(key) is not False for key in (
                "buy_authorized", "orders_authorized", "publish_authorized",
            )):
                raise ReconciliationError("Snapshot not certified as shadow-only")
            if obj.get("model") != "EXPERIMENTAL_NOT_CERTIFIED / raw Elo (calibration not integrated)":
                raise ReconciliationError("Unknown baseline model")
            recorded = instant(obj["captured_at_utc"])
            if recorded > now.astimezone(UTC) + timedelta(minutes=5):
                raise ReconciliationError("Future snapshot")
            rows = obj.get("screened")
            if not isinstance(rows, list):
                raise ReconciliationError("Missing screened forecasts")
            for row in rows:
                if not isinstance(row, dict):
                    raise ReconciliationError("Malformed screened record")
                game_id, matchup = row.get("game_id"), row.get("matchup")
                event = row.get("event_ticker")
                start = instant(row.get("start_utc"))
                p = row.get("raw_elo_home_probability")
                if (
                    not isinstance(game_id, str) or not GAME_ID.fullmatch(game_id)
                    or not isinstance(matchup, str) or matchup.count("@") != 1
                    or not all(TEAM.fullmatch(t) for t in matchup.split("@"))
                    or matchup.split("@")[0] == matchup.split("@")[1]
                    or not isinstance(event, str) or not TICKER.fullmatch(event)
                    or type(p) not in (float, int) or not math.isfinite(p)
                    or not 0 < p < 1
                    or start <= recorded
                    or not isinstance(row.get("positions"), list)
                    or len(row["positions"]) != 4
                    or any(
                        not isinstance(pos, dict)
                        or pos.get("action") not in ("WATCH", "PASS")
                        for pos in row["positions"]
                    )
                ):
                    raise ReconciliationError("Invalid/future-leaking pregame forecast")
                away_code, home_code = matchup.split("@")
                ticker_date = start.astimezone(ET).strftime("%y%b%d").upper()
                # Observed 2026 KXNHLGAME events encode away then home.
                # Reversing teams is not a valid reconciliation identity.
                if event != f"KXNHLGAME-{ticker_date}{away_code}{home_code}":
                    raise ReconciliationError("Market ticker contradicts NHL game identity")
                old_identity = identity.get(game_id)
                new_identity = (matchup, event, start)
                if old_identity is not None and old_identity != new_identity:
                    raise ReconciliationError("Conflicting immutable game identity")
                identity[game_id] = new_identity
                entry = FrozenForecast(
                    game_id, matchup, event, start, recorded,
                    float(p), path.name,
                )
                old = forecasts.get(game_id)
                if old is None or (entry.forecast_captured_utc, entry.snapshot_name) < (
                    old.forecast_captured_utc, old.snapshot_name
                ):
                    forecasts[game_id] = entry
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ReconciliationError(
                f"Unusable archive {path.name}"
            ) from exc
    return forecasts


def scoreboard_date(forecast: FrozenForecast) -> str:
    """NHL date must be ET-local, not UTC (e.g. 00:00 UTC = prior ET date)."""
    return forecast.start_utc.astimezone(ET).date().isoformat()


def pending_date(
    forecasts: dict[str, FrozenForecast], reconciled_ids: set[str],
    *, now: datetime,
) -> str | None:
    """Earliest matured, unreconciled ET game date; at most one request/run."""
    dates = [
        scoreboard_date(f)
        for gid, f in forecasts.items()
        if gid not in reconciled_ids and now >= f.start_utc + MIN_AFTER_START
    ]
    return min(dates) if dates else None


def official_final(
    raw: dict[str, Any],
    forecast: FrozenForecast,
    *,
    requested_date: str,
) -> dict[str, Any] | None:
    """Validate exact NHL ID, ET date, participants, season and final scores.

    OFF and FINAL are full-game results incl. OT/shootout. In-progress or
    postponed games are pending and never interpreted as losses.
    """
    if not isinstance(raw, dict) or str(raw.get("id")) != forecast.game_id.removeprefix("nhl:"):
        raise ReconciliationError("Official NHL game ID mismatch")
    if raw.get("gameState") not in ("FINAL", "OFF"):
        return None
    if raw.get("gameType") != 2:
        raise ReconciliationError("Not NHL regular season")
    year = int(requested_date[:4])
    season_year = year if int(requested_date[5:7]) >= 7 else year - 1
    if str(raw.get("season")) != f"{season_year}{season_year + 1}":
        raise ReconciliationError("Official season mismatch")
    if raw.get("gameDate") != requested_date:
        raise ReconciliationError("Official gameDate mismatch")
    start = instant(raw.get("startTimeUTC"))
    if start != forecast.start_utc or scoreboard_date(forecast) != requested_date:
        raise ReconciliationError("Original fixture start contradicts official final")
    away, home = raw.get("awayTeam"), raw.get("homeTeam")
    if not isinstance(away, dict) or not isinstance(home, dict):
        raise ReconciliationError("Missing official final teams")
    if away.get("abbrev") != forecast.away or home.get("abbrev") != forecast.home:
        raise ReconciliationError("Official team identity mismatch")
    a, h = away.get("score"), home.get("score")
    if type(a) is not int or type(h) is not int or not 0 <= a < 50 or not 0 <= h < 50 or h == a:
        raise ReconciliationError("Invalid or tied full-game final")
    return {
        "away_score": a,
        "home_score": h,
        "winner": forecast.home if h > a else forecast.away,
        "home_won": h > a,
        "state": raw["gameState"],
    }


def reconcile_day(
    forecasts: dict[str, FrozenForecast],
    scoreboard: dict[str, Any],
    *,
    requested_date: str,
    reconciled_ids: set[str],
    captured_at: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(scoreboard, dict) or scoreboard.get("currentDate") != requested_date:
        raise ReconciliationError("Scoreboard date mismatch")
    games = scoreboard.get("games")
    if not isinstance(games, list):
        raise ReconciliationError("Malformed official NHL scoreboard")
    mapping: dict[str, dict] = {}
    for raw in games:
        if not isinstance(raw, dict) or type(raw.get("id")) is not int:
            raise ReconciliationError("Malformed official NHL game entry")
        gid = f"nhl:{raw['id']}"
        if gid in mapping:
            raise ReconciliationError("Duplicate official NHL game ID")
        mapping[gid] = raw
    scorerows, pending = [], []
    for game_id, f in sorted(forecasts.items()):
        if game_id in reconciled_ids or scoreboard_date(f) != requested_date:
            continue
        if captured_at < f.start_utc + MIN_AFTER_START:
            continue
        raw = mapping.get(game_id)
        if raw is None:
            pending.append(game_id)
            continue
        final = official_final(raw, f, requested_date=requested_date)
        if final is None:
            pending.append(game_id)
            continue
        p = f.home_probability
        truth = int(final["home_won"])
        brier = (p - truth) ** 2
        logloss = -math.log(p if truth else 1 - p)
        scorerows.append({
            "game_id": game_id,
            "matchup": f.matchup,
            "event_ticker": f.event_ticker,
            "pregame_snapshot": f.snapshot_name,
            "forecast_captured_utc": f.forecast_captured_utc.isoformat(),
            "official_start_utc": f.start_utc.isoformat(),
            "official_score_date_et": requested_date,
            "final_observed_at_utc": captured_at.isoformat(),
            "official_source": f"https://api-web.nhle.com/v1/score/{requested_date}",
            "model_status": "EXPERIMENTAL_NOT_CERTIFIED",
            "home_probability": p,
            **final,
            "brier": round(brier, 12),
            "log_loss": round(logloss, 12),
            "publication_authorized": False,
            "buy_authorized": False,
        })
    return scorerows, pending


def summary(rows: list[dict[str, Any]]) -> dict[str, float | int | None]:
    if not rows:
        return {
            "n": 0, "brier": None, "log_loss": None,
            "home_win_rate": None, "ece_10": None,
        }
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(10)]
    for row in rows:
        buckets[min(9, int(10 * row["home_probability"]))].append(row)
    ece = sum(
        len(bucket) / len(rows) * abs(
            sum(int(r["home_won"]) for r in bucket) / len(bucket)
            - sum(r["home_probability"] for r in bucket) / len(bucket)
        ) for bucket in buckets if bucket
    )
    return {
        "n": len(rows),
        "brier": round(sum(r["brier"] for r in rows) / len(rows), 8),
        "log_loss": round(sum(r["log_loss"] for r in rows) / len(rows), 8),
        "home_win_rate": round(sum(int(r["home_won"]) for r in rows) / len(rows), 8),
        "ece_10": round(ece, 8),
    }


def read_reconciled_ids(directory: Path) -> set[str]:
    if not directory.exists():
        return set()
    result: set[str] = set()
    for path in directory.glob("nhl-*.json"):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            gid = obj.get("game_id") if isinstance(obj, dict) else None
            if not isinstance(gid, str) or not GAME_ID.fullmatch(gid):
                raise ReconciliationError("Malformed reconciliation ledger")
            if path.name != f"nhl-{gid.removeprefix('nhl:')}.json":
                raise ReconciliationError("Ledger filename/game identity mismatch")
            if gid in result:
                raise ReconciliationError("Duplicate ledger entry")
            p = obj.get("home_probability")
            h = obj.get("home_won")
            matchup = obj.get("matchup")
            if (
                type(p) not in (int, float) or not math.isfinite(p)
                or not 0 < p < 1
                or type(h) is not bool
                or not isinstance(matchup, str) or matchup.count("@") != 1
                or obj.get("winner") != matchup.split("@")[int(h)]
                or obj.get("buy_authorized") is not False
                or obj.get("publication_authorized") is not False
                or obj.get("model_status") != "EXPERIMENTAL_NOT_CERTIFIED"
                or type(obj.get("home_score")) is not int
                or type(obj.get("away_score")) is not int
                or (obj["home_score"] > obj["away_score"]) != h
                or type(obj.get("brier")) not in (float, int)
                or type(obj.get("log_loss")) not in (float, int)
                or not math.isfinite(obj["brier"])
                or not math.isfinite(obj["log_loss"])
                or abs(obj["brier"] - (p - int(h)) ** 2) > 1e-9
                or abs(obj["log_loss"] + math.log(p if h else 1 - p)) > 1e-9
                or instant(obj.get("forecast_captured_utc"))
                   >= instant(obj.get("official_start_utc"))
            ):
                raise ReconciliationError("Corrupt or unauthorized reconciliation ledger")
            result.add(gid)
        except ReconciliationError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ReconciliationError("Cannot trust existing reconciliation ledger") from exc
    return result



def accumulated_summary(directory: Path) -> dict[str, float | int | None]:
    """Prospective-only performance; no historical evaluation or certification."""
    ids = read_reconciled_ids(directory)
    rows = [
        json.loads((directory / f"nhl-{gid.removeprefix('nhl:')}.json").read_text(
            encoding="utf-8"
        ))
        for gid in sorted(ids)
    ]
    return summary(rows)
