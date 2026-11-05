#!/usr/bin/env python3
"""Research-only NBA shadow reconciliation. No production or model writes."""
from __future__ import annotations
import argparse
import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
TEAM_ALIAS = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS"}

def canonical(team: str) -> str:
    code = team.strip().upper()
    return TEAM_ALIAS.get(code, code)

def parse_stamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return stamp.astimezone(UTC)

def archived_forecasts(directory: Path) -> tuple[dict, dict]:
    """Freeze earliest valid pre-tipoff forecast per ET-day matchup."""
    chosen = {}
    counts = Counter()
    for path in sorted(directory.glob("nba-shadow-*.json")):
        counts["snapshots"] += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            captured = parse_stamp(payload["captured_at_utc"])
            rows = payload["screened"]
            if not isinstance(rows, list):
                raise ValueError("screened not list")
        except (OSError, ValueError, TypeError, KeyError):
            counts["malformed_snapshots"] += 1
            continue
        for item in rows:
            counts["screened_rows"] += 1
            try:
                away, home = [canonical(part) for part in item["matchup"].split("@")]
                kickoff = parse_stamp(item["start_utc"])
                p = float(item["raw_elo_home_probability"])
                if not (0 <= p <= 1 and math.isfinite(p)) or not away or not home or away == home:
                    raise ValueError("invalid forecast")
                if captured >= kickoff:
                    counts["post_tipoff_rows"] += 1
                    continue
                key = (kickoff.astimezone(ET).strftime("%Y%m%d"), away, home)
                candidate = {"captured_at_utc": captured.isoformat(),
                             "kickoff_utc": kickoff.isoformat(),
                             "home_probability": p, "source_file": path.name}
                if key not in chosen or candidate["captured_at_utc"] < chosen[key]["captured_at_utc"]:
                    chosen[key] = candidate
            except (KeyError, ValueError, TypeError, AttributeError):
                counts["invalid_rows"] += 1
    return chosen, dict(counts)

def scoreboard_results(day: str, *, fetch=None) -> dict:
    """Only completed scoreboard finals; never use market settlement as game result."""
    if fetch is None:
        def fetch(date_string: str) -> dict:
            url = ("https://site.api.espn.com/apis/site/v2/"
                   "sports/basketball/nba/scoreboard?dates=" + date_string)
            request = Request(url, headers={"User-Agent": "PARALLAX-NBA-Research/1"})
            with urlopen(request, timeout=15) as response:
                return json.load(response)
    results = {}
    for game in fetch(day).get("events", []):
        if not game.get("status", {}).get("type", {}).get("completed"):
            continue
        competitions = game.get("competitions", [])
        if len(competitions) != 1:
            continue
        side = {row.get("homeAway"): row for row in competitions[0].get("competitors", [])}
        if set(side) != {"home", "away"}:
            continue
        try:
            home = canonical(side["home"]["team"]["abbreviation"])
            away = canonical(side["away"]["team"]["abbreviation"])
            hs, ans = int(side["home"]["score"]), int(side["away"]["score"])
            if hs == ans or hs < 0 or ans < 0:
                continue
            key = (day, away, home)
            if key in results and results[key]["scores"] != [ans, hs]:
                raise ValueError("contradictory scoreboard results")
            results[key] = {"scores": [ans, hs], "home_won": hs > ans,
                            "source": "ESPN_PUBLIC_SCOREBOARD",
                            "source_game_id": str(game.get("id", ""))}
        except (KeyError, ValueError, TypeError):
            continue
    return results

def evaluate(forecasts: dict, *, now: datetime, fetch=None) -> dict:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    finals, failures = {}, {}
    days = sorted({key[0] for key, row in forecasts.items()
                   if parse_stamp(row["kickoff_utc"]) <= now})
    for day in days:
        try:
            finals.update(scoreboard_results(day, fetch=fetch))
        except (OSError, ValueError, KeyError, TypeError, TimeoutError) as exc:
            failures[day] = type(exc).__name__
    graded, unmatched = [], []
    for (day, away, home), forecast in sorted(forecasts.items()):
        key = (day, away, home)
        row = {"et_day": day, "matchup": f"{away}@{home}", **forecast}
        kickoff = parse_stamp(forecast["kickoff_utc"])
        if key in finals:
            actual = int(finals[key]["home_won"])
            p = forecast["home_probability"]
            row.update(finals[key])
            row.update({"status": "GRADED", "correct": (p >= 0.5) == bool(actual),
                        "brier": (p - actual)**2})
            graded.append(row)
        else:
            row["status"] = ("FUTURE" if kickoff > now else
                             "SCOREBOARD_ERROR" if day in failures else "MISSING_FINAL_OR_IDENTITY")
            unmatched.append(row)
    n = len(graded)
    bins = [[r for r in graded if min(9, int(r["home_probability"]*10)) == b]
            for b in range(10)]
    ece = sum((len(group)/n)*abs(sum(int(r["home_won"]) for r in group)/len(group)
              - sum(r["home_probability"] for r in group)/len(group))
              for group in bins if group) if n else None
    correct = sum(bool(r["correct"]) for r in graded)
    return {"status": "RESEARCH_ONLY_NOT_CERTIFICATION",
            "unique_forecasts": len(forecasts), "graded_games": n,
            "correct": correct, "accuracy": correct/n if n else None,
            "brier": sum(r["brier"] for r in graded)/n if n else None,
            "ece_10_bins": ece, "baseline_brier_50_50": 0.25,
            "unmatched_counts": dict(sorted(Counter(r["status"] for r in unmatched).items())),
            "scoreboard_errors": failures, "games": graded, "unmatched": unmatched,
            "model_unchanged": True, "buy_authorized": False,
            "independent_source_verified": False}

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", type=Path,
                        default=Path.home()/"parallax-research/nba/forward")
    parser.add_argument("--output", type=Path, help="Optional research report destination")
    args = parser.parse_args()
    forecasts, source = archived_forecasts(args.snapshots)
    if not forecasts:
        parser.error("No eligible pre-tipoff NBA forecasts found")
    report = evaluate(forecasts, now=datetime.now(UTC))
    report["source_counts"] = source
    print(json.dumps({k:v for k,v in report.items() if k not in ("games", "unmatched")},
                     indent=2, sort_keys=True))
    if args.output:
        if args.output.exists():
            parser.error("Output already exists; refusing overwrite")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True)+"\n",
                               encoding="utf-8")
        print("Research report:", args.output)
    return 0 if report["graded_games"] and not report["scoreboard_errors"] else 2

if __name__ == "__main__":
    raise SystemExit(main())
