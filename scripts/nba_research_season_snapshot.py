#!/usr/bin/env python3
"""Freeze 2026 NBA regular-season research finals from public scoreboards.

No model/runtime updates. Provider coverage, independence and permissions are
NOT verified by this artifact; it must not be used for BUY certification.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from nba_forward_reconcile import canonical

FIELDS = ("sport", "game_id", "season", "game_date", "home_team", "away_team", "winner_team")

def snapshot(start: date, end: date, *, fetch) -> tuple[list[dict], dict]:
    if not (date(2026, 10, 1) <= start <= end < date(2027, 7, 1)):
        raise ValueError("expected explicit 2026 season date window")
    results, days, seen = [], [], set()
    day = start
    while day <= end:
        stamp = day.strftime("%Y%m%d")
        payload = fetch(stamp)
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise ValueError(f"invalid response for {stamp}")
        accepted = 0
        regular = 0
        unresolved = 0
        event_ids = set()
        for game in payload["events"]:
            if not isinstance(game, dict) or not isinstance(game.get("season"), dict):
                raise ValueError(f"malformed scoreboard event for {stamp}")
            if game["season"].get("type") != 2:
                continue
            event_id = str(game.get("id") or "")
            if not event_id or event_id in event_ids:
                raise ValueError(f"missing or duplicate event identity for {stamp}")
            event_ids.add(event_id)
            regular += 1
            status = game.get("status", {}).get("type")
            if not isinstance(status, dict) or type(status.get("completed")) is not bool:
                raise ValueError(f"unknown game completion status for {stamp}")
            if not status["completed"]:
                unresolved += 1
                continue
            competitions = game.get("competitions", [])
            if len(competitions) != 1:
                raise ValueError(f"invalid competition for {stamp}")
            c = {r.get("homeAway"): r for r in competitions[0].get("competitors", [])}
            if set(c) != {"home", "away"}:
                raise ValueError(f"invalid competitors for {stamp}")
            home = canonical(c["home"]["team"]["abbreviation"])
            away = canonical(c["away"]["team"]["abbreviation"])
            hs, ans = int(c["home"]["score"]), int(c["away"]["score"])
            game_id = str(game["id"])
            if hs == ans or hs < 0 or ans < 0 or home == away or not home or not away:
                raise ValueError(f"invalid final for {stamp}")
            identity = (stamp, away, home)
            if identity in seen:
                raise ValueError(f"duplicate NBA fixture {identity}")
            seen.add(identity)
            results.append({"sport": "NBA", "game_id": f"espn:{game_id}",
                            "season": "2026", "game_date": day.isoformat(),
                            "home_team": home, "away_team": away,
                            "winner_team": home if hs > ans else away})
            accepted += 1
        days.append({"day": stamp, "finals": accepted,
                     "provider_regular_events": regular,
                     "unresolved_events": unresolved,
                     "provider_internal_complete": unresolved == 0})
        day += timedelta(days=1)
    return results, {"source": "ESPN_PUBLIC_SCOREBOARD_RESEARCH_ONLY",
                     "independent_source_verified": False,
                     "source_authorization_verified": False,
                     "complete_season_verified": False,
                     "start_date": start.isoformat(), "end_date": end.isoformat(),
                     "dates_requested": len(days), "games": len(results),
                     "all_days_provider_internal_complete": all(d["provider_internal_complete"] for d in days),
                     "expected_schedule_reconciled": False,
                     "daily_counts": days}

def fetch_day(day: str) -> dict:
    from urllib.request import Request, urlopen
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=" + day
    request = Request(url, headers={"User-Agent": "PARALLAX-NBA-Research/1"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 10, 20))
    parser.add_argument("--through", type=date.fromisoformat,
                        default=datetime.now(UTC).date() - timedelta(days=1))
    parser.add_argument("--csv", type=Path, required=True)
    args = parser.parse_args()
    manifest = args.csv.with_suffix(".provenance.json")
    if args.csv.exists() or manifest.exists():
        parser.error("Output exists; refusing overwrite")
    rows, provenance = snapshot(args.start, args.through, fetch=fetch_day)
    if not rows:
        parser.error("No completed games; refusing empty history")
    args.csv.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    content = buffer.getvalue().encode("utf-8")
    provenance["csv_sha256"] = hashlib.sha256(content).hexdigest()
    provenance["generated_at_utc"] = datetime.now(UTC).isoformat()
    with args.csv.open("xb") as stream:
        stream.write(content)
    with manifest.open("x", encoding="utf-8") as stream:
        json.dump(provenance, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"csv": str(args.csv), "provenance": str(manifest),
                      "games": len(rows), "status": "RESEARCH_ONLY"}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
