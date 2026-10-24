"""Offline-only NBA frozen pregame research against verified BDL final results.

Only independently archived, pre-tipoff, research-only NBA forecasts count.
No provider calls, trading, public feed, scheduler, alerts, or model promotion.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MAX_SNAPSHOTS = 1000
MIN_AFTER_START = timedelta(hours=4)
GAME_ID = re.compile(r"bdl:([1-9][0-9]*)\Z")
TEAM = re.compile(r"[A-Z]{2,4}\Z")
MODEL = "EXPERIMENTAL_NOT_CERTIFIED / raw Elo"


class ReconciliationError(ValueError):
    """Untrusted archival data, fixture, or final outcome; fail closed."""


def instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ReconciliationError("Missing timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError("Naive timestamp")
        return result.astimezone(UTC)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ReconciliationError("Invalid timestamp") from exc


@dataclass(frozen=True)
class FrozenForecast:
    game_id: str
    matchup: str
    event_ticker: str
    start_utc: datetime
    captured_utc: datetime
    home_probability: float
    snapshot_name: str

    @property
    def away(self) -> str:
        return self.matchup.split("@")[0]

    @property
    def home(self) -> str:
        return self.matchup.split("@")[1]

    @property
    def date_et(self) -> str:
        return self.start_utc.astimezone(ET).date().isoformat()


def archived_forecasts(directory: Path, *, now: datetime) -> dict[str, FrozenForecast]:
    """Earliest legitimate pregame forecast for each game, never cherry-picked."""
    moment = instant(now.isoformat())
    if not directory.exists():
        return {}
    paths = sorted(directory.glob("nba-shadow-*.json"))
    if len(paths) > MAX_SNAPSHOTS:
        raise ReconciliationError("NBA archive capacity exceeded; fail closed")
    result: dict[str, FrozenForecast] = {}
    game_identity: dict[str, tuple[str, str, datetime]] = {}
    for path in paths:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(doc, dict) or doc.get("mode") != "RESEARCH_ONLY"
                    or doc.get("sport") != "NBA" or doc.get("model") != MODEL
                    or any(doc.get(name) is not False for name in (
                        "buy_authorized", "orders_authorized", "publish_authorized",
                        "alerts_authorized",
                    ))):
                raise ReconciliationError("Invalid NBA research envelope")
            captured = instant(doc.get("captured_at_utc"))
            if captured > moment + timedelta(minutes=5):
                raise ReconciliationError("Future-dated NBA research archive")
            rows = doc.get("screened")
            if not isinstance(rows, list):
                raise ReconciliationError("NBA screened records missing")
            for row in rows:
                if not isinstance(row, dict):
                    raise ReconciliationError("Malformed NBA game forecast")
                gid, matchup, ticker = (row.get("game_id"),
                                        row.get("matchup"), row.get("event_ticker"))
                if (not isinstance(gid, str) or not GAME_ID.fullmatch(gid)
                        or not isinstance(matchup, str) or matchup.count("@") != 1
                        or any(not TEAM.fullmatch(code) for code in matchup.split("@"))
                        or matchup.split("@")[0] == matchup.split("@")[1]):
                    raise ReconciliationError("NBA game identity invalid")
                start = instant(row.get("start_utc"))
                p = row.get("raw_elo_home_probability")
                expected = (f"KXNBAGAME-{start.astimezone(ET):%y%b%d}"
                            f"{matchup.split('@')[0]}{matchup.split('@')[1]}").upper()
                positions = row.get("positions")
                if (ticker != expected
                        or start <= captured
                        or type(p) not in (int, float) or not math.isfinite(p)
                        or not 0 < p < 1
                        or not isinstance(positions, list) or len(positions) != 4
                        or {
                            (position.get("ticker"), position.get("side"))
                            for position in positions if isinstance(position, dict)
                        } != {
                            (f"{ticker}-{matchup.split('@')[0]}", "YES"),
                            (f"{ticker}-{matchup.split('@')[0]}", "NO"),
                            (f"{ticker}-{matchup.split('@')[1]}", "YES"),
                            (f"{ticker}-{matchup.split('@')[1]}", "NO"),
                        }
                        or any(
                            not isinstance(pos, dict)
                            or pos.get("action") not in ("WATCH", "PASS")
                            for pos in positions
                        )):
                    raise ReconciliationError("NBA market or pregame forecast invalid")
                identity = (matchup, ticker, start)
                if gid in game_identity and game_identity[gid] != identity:
                    raise ReconciliationError("Contradictory NBA immutable game identity")
                game_identity[gid] = identity
                forecast = FrozenForecast(
                    gid, matchup, ticker, start, captured, float(p), path.name,
                )
                prior = result.get(gid)
                if prior is None or (captured, path.name) < (
                        prior.captured_utc, prior.snapshot_name):
                    result[gid] = forecast
        except ReconciliationError:
            raise
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            raise ReconciliationError(f"NBA archive unreadable: {path.name}") from exc
    return result


def pending_date(
    forecasts: dict[str, FrozenForecast], reconciled_ids: set[str],
    *, now: datetime, last_attempts: dict[str, datetime] | None = None,
) -> str | None:
    current = instant(now.isoformat())
    pending = {
        forecast.date_et
        for gid, forecast in forecasts.items()
        if gid not in reconciled_ids and current >= forecast.start_utc + MIN_AFTER_START
    }
    if not pending:
        return None
    previous = last_attempts or {}
    unchecked = sorted(pending - set(previous))
    return unchecked[0] if unchecked else min(pending, key=lambda day: (previous[day], day))


def reconcile_day(
    forecasts: dict[str, FrozenForecast], payload: dict, *,
    requested_date: str, reconciled_ids: set[str], observed: datetime,
    regular_query_verified: bool,
) -> tuple[list[dict], list[str]]:
    """Check saved BDL regular-season games response; NO network requests."""
    if not regular_query_verified:
        raise ReconciliationError("Verified NBA regular-season query required")
    current = instant(observed.isoformat())
    try:
        datetime.strptime(requested_date, "%Y-%m-%d")
    except (ValueError, TypeError) as exc:
        raise ReconciliationError("Invalid NBA reconciliation date") from exc
    if not isinstance(payload, dict):
        raise ReconciliationError("Malformed NBA final source")
    rows, meta = payload.get("data"), payload.get("meta")
    if (not isinstance(rows, list) or len(rows) > 100 or not isinstance(meta, dict)
            or meta.get("next_cursor") is not None
            or ("next_cursor" not in meta and len(rows) == 100)):
        raise ReconciliationError("NBA final source page incomplete")
    by_id: dict[str, dict] = {}
    for raw in rows:
        if not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] <= 0:
            raise ReconciliationError("NBA final source ID malformed")
        gid = f"bdl:{raw['id']}"
        if gid in by_id or raw.get("date") != requested_date:
            raise ReconciliationError("Duplicate or wrong-date NBA source entry")
        by_id[gid] = raw
    settled, awaiting = [], []
    for gid, forecast in sorted(forecasts.items()):
        if gid in reconciled_ids or forecast.date_et != requested_date:
            continue
        if current < forecast.start_utc + MIN_AFTER_START:
            continue
        raw = by_id.get(gid)
        if raw is None or raw.get("status_state") != "final":
            awaiting.append(gid)
            continue
        season = int(requested_date[:4]) if int(requested_date[5:7]) >= 7 else int(requested_date[:4]) - 1
        home, away = raw.get("home_team"), raw.get("visitor_team")
        if (raw.get("season") != season or raw.get("postseason") is not False
                or raw.get("postponed") is True
                or raw.get("season_type", "regular") != "regular"
                or raw.get("ist_stage") == "championship"
                or instant(raw.get("datetime")) != forecast.start_utc
                or not isinstance(home, dict) or not isinstance(away, dict)
                or home.get("abbreviation") != forecast.home
                or away.get("abbreviation") != forecast.away):
            raise ReconciliationError("NBA final contradicts frozen game identity")
        hs, as_ = raw.get("home_team_score"), raw.get("visitor_team_score")
        if (type(hs) is not int or type(as_) is not int
                or not 0 <= hs <= 250 or not 0 <= as_ <= 250 or hs == as_):
            raise ReconciliationError("Invalid NBA full-game final scores")
        won = hs > as_
        p = forecast.home_probability
        settled.append({
            "game_id": gid, "matchup": forecast.matchup,
            "event_ticker": forecast.event_ticker,
            "pregame_snapshot": forecast.snapshot_name,
            "forecast_captured_utc": forecast.captured_utc.isoformat(),
            "official_start_utc": forecast.start_utc.isoformat(),
            "game_date_et": requested_date, "final_observed_utc": current.isoformat(),
            "result_source": "BALLDONTLIE regular-season saved games response",
            "model_status": "EXPERIMENTAL_NOT_CERTIFIED",
            "home_probability": p, "home_score": hs, "away_score": as_,
            "home_won": won, "winner": forecast.home if won else forecast.away,
            "brier": round((p - int(won))**2, 12),
            "log_loss": round(-math.log(p if won else 1 - p), 12),
            "buy_authorized": False, "publication_authorized": False,
            "orders_authorized": False,
        })
    return settled, awaiting


def summary(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "brier": None, "log_loss": None, "ece_10": None}
    bins: list[list[dict]] = [[] for _ in range(10)]
    for row in rows:
        bins[min(9, int(row["home_probability"]*10))].append(row)
    ece = sum(len(b)/len(rows) * abs(
        sum(int(r["home_won"]) for r in b)/len(b)
        - sum(r["home_probability"] for r in b)/len(b)
    ) for b in bins if b)
    return {
        "n": len(rows),
        "brier": round(sum(r["brier"] for r in rows)/len(rows), 8),
        "log_loss": round(sum(r["log_loss"] for r in rows)/len(rows), 8),
        "ece_10": round(ece, 8),
    }


def read_reconciled_ids(directory: Path) -> set[str]:
    if not directory.exists():
        return set()
    result: set[str] = set()
    for path in directory.glob("nba-*.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            gid = doc.get("game_id") if isinstance(doc, dict) else None
            if (not isinstance(gid, str) or not GAME_ID.fullmatch(gid)
                    or path.name != f"nba-{gid.removeprefix('bdl:')}.json"
                    or gid in result):
                raise ReconciliationError("NBA reconciliation ledger identity invalid")
            p, won = doc.get("home_probability"), doc.get("home_won")
            matchup = doc.get("matchup")
            if (type(p) not in (float, int) or not math.isfinite(p) or not 0 < p < 1
                    or type(won) is not bool
                    or not isinstance(matchup, str) or matchup.count("@") != 1
                    or doc.get("winner") != matchup.split("@")[int(won)]
                    or doc.get("model_status") != "EXPERIMENTAL_NOT_CERTIFIED"
                    or any(doc.get(k) is not False for k in (
                        "buy_authorized", "publication_authorized", "orders_authorized"))
                    or type(doc.get("home_score")) is not int
                    or type(doc.get("away_score")) is not int
                    or doc["home_score"] == doc["away_score"]
                    or (doc["home_score"] > doc["away_score"]) != won
                    or type(doc.get("brier")) not in (int, float)
                    or type(doc.get("log_loss")) not in (int, float)
                    or not math.isfinite(doc["brier"]) or not math.isfinite(doc["log_loss"])
                    or abs(doc["brier"] - (p - int(won))**2) > 1e-9
                    or abs(doc["log_loss"] + math.log(p if won else 1-p)) > 1e-9
                    or instant(doc.get("forecast_captured_utc"))
                       >= instant(doc.get("official_start_utc"))):
                raise ReconciliationError("NBA reconciliation ledger corrupted")
            result.add(gid)
        except ReconciliationError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ReconciliationError("NBA reconciliation ledger unreadable") from exc
    return result


def accumulated_summary(directory: Path) -> dict:
    ids = read_reconciled_ids(directory)
    rows = [
        json.loads((directory / f"nba-{gid.removeprefix('bdl:')}.json").read_text(encoding="utf-8"))
        for gid in sorted(ids)
    ]
    return summary(rows)
