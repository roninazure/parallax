#!/usr/bin/env python3
"""Parse a locally extracted official NBA by-date schedule for offline research.

Input is text from the NBA 2026-27 Schedule By Date PDF (pdftotext -layout).
No network access. A published schedule is SUBJECT TO CHANGE and does not
verify final outcomes, provider rights, or BUY eligibility.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
from datetime import date, datetime, UTC
from pathlib import Path

TEAMS = {
    "Atlanta": "ATL", "Boston": "BOS", "Brooklyn": "BKN",
    "Charlotte": "CHA", "Chicago": "CHI", "Cleveland": "CLE",
    "Dallas": "DAL", "Denver": "DEN", "Detroit": "DET",
    "Golden State": "GSW", "Houston": "HOU", "Indiana": "IND",
    "LA Clippers": "LAC", "LA Lakers": "LAL", "Memphis": "MEM",
    "Miami": "MIA", "Milwaukee": "MIL", "Minnesota": "MIN",
    "New Orleans": "NOP", "New York": "NYK", "Oklahoma City": "OKC",
    "Orlando": "ORL", "Philadelphia": "PHI", "Phoenix": "PHX",
    "Portland": "POR", "Sacramento": "SAC", "San Antonio": "SAS",
    "Toronto": "TOR", "Utah": "UTA", "Washington": "WAS",
}
LINE = re.compile(
    r"^\s*\d+\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\.\s+"
    r"(\d{1,2}/\d{1,2}/\d{2})\s+(.+?)\s+at\s+(.+?)\s+"
    r"\d{1,2}:\d{2}\s*(?:AM|PM)\b"
)
SOURCE_URL = ("https://ak-static.cms.nba.com/wp-content/uploads/sites/46/"
              "2026/08/2026-27-NBA-Regular-Season-Schedule-By-Date.pdf")
FIELDS = ("game_date", "home_team", "away_team", "status")


def extract(text: str, start: date, end: date) -> tuple[list[dict], dict]:
    if not (date(2026, 10, 20) <= start <= end <= date(2026, 11, 30)):
        raise ValueError("unexpected bounded 2026 schedule date range")
    rows, seen, matched = [], set(), 0
    for line in text.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        day = datetime.strptime(m[1], "%m/%d/%y").date()
        if not start <= day <= end:
            continue
        matched += 1
        away, home = TEAMS.get(m[2].strip()), TEAMS.get(m[3].strip())
        if not away or not home or away == home:
            raise ValueError("unrecognized official schedule team: " + line[:120])
        fixture = (day, away, home)
        if fixture in seen:
            raise ValueError("duplicate official schedule fixture")
        seen.add(fixture)
        # FINAL is expected for this historical comparison window, NOT an
        # official statement of outcome; ESPN/BDL independently supply finals.
        rows.append({"game_date": day.isoformat(), "home_team": home,
                     "away_team": away, "status": "FINAL"})
    if not rows or len(rows) != matched:
        raise ValueError("no valid official schedule rows in requested window")
    rows.sort(key=lambda row: (row["game_date"], row["away_team"], row["home_team"]))
    return rows, {"schedule_source": "NBA_OFFICIAL_2026_27_BY_DATE_PDF",
                  "source_url": SOURCE_URL,
                  "published_as_of": "2026-08-13",
                  "subject_to_change": True,
                  "historical_expected_final_status_inferred": True,
                  "source_authorization_verified": False,
                  "full_schedule_coverage_verified": False,
                  "buy_authorized": False,
                  "coverage_start": start.isoformat(), "coverage_end": end.isoformat(),
                  "fixtures": len(rows), "network_requests": 0}


def write_once(path: Path, data: bytes) -> None:
    if path.is_symlink() or path.exists():
        raise ValueError("output already exists or is symlink")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--schedule-text", required=True, type=Path)
    p.add_argument("--csv", required=True, type=Path)
    p.add_argument("--start", type=date.fromisoformat, default=date(2026, 10, 22))
    p.add_argument("--through", type=date.fromisoformat, default=date(2026, 10, 31))
    a = p.parse_args()
    if a.schedule_text.is_symlink() or not a.schedule_text.is_file():
        p.error("schedule text missing or symlinked")
    content = a.schedule_text.read_bytes()
    rows, provenance = extract(content.decode("utf-8"), a.start, a.through)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    data = buffer.getvalue().encode("utf-8")
    provenance["csv_sha256"] = hashlib.sha256(data).hexdigest()
    provenance["text_sha256"] = hashlib.sha256(content).hexdigest()
    provenance["created_at_utc"] = datetime.now(UTC).isoformat()
    manifest = a.csv.with_suffix(".provenance.json")
    if a.csv.exists() or manifest.exists() or a.csv.is_symlink() or manifest.is_symlink():
        p.error("output already exists; refusing overwrite")
    write_once(a.csv, data)
    write_once(manifest, (json.dumps(provenance, sort_keys=True, indent=2) + "\n").encode())
    print(json.dumps({"status": "OFFICIAL_SCHEDULE_RESEARCH_ONLY",
                      "fixtures": len(rows), "csv": str(a.csv),
                      "provenance": str(manifest), "network_requests": 0},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
