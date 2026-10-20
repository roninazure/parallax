#!/usr/bin/env python3
"""Bounded, resumable NBA regular-season snapshot downloader for OFFLINE research.

Uses BALLDONTLIE's free games endpoint. No production imports or writes.
No retries (including HTTP 429); sleeps between requests below free tier.
Run one season at a time, with a hard page ceiling and local page cache.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_URL = "https://api.balldontlie.io/v1/games"
PER_PAGE = 100
SECONDS_BETWEEN_REQUESTS = 15


def validate_page(payload: object, season: int) -> tuple[list[dict], int | None]:
    if not isinstance(payload, dict):
        raise ValueError("Unexpected provider payload")
    rows, meta = payload.get("data"), payload.get("meta")
    if not isinstance(rows, list) or not isinstance(meta, dict) or len(rows) > PER_PAGE:
        raise ValueError("Malformed page data/meta or excessive page length")
    if not rows:
        raise ValueError("Empty page: cannot verify historical completeness")
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int:
            raise ValueError("Malformed game/ID")
        if row.get("season") != season:
            raise ValueError("Season mismatch in provider response")
        if row.get("postseason") is True:
            raise ValueError("Postseason result in regular-season query")
    # BALLDONTLIE may omit next_cursor on the terminal page instead of
    # returning JSON null. Accept omission ONLY for a short page, to avoid
    # silently truncating a full page with missing pagination metadata.
    if "next_cursor" not in meta:
        if len(rows) == PER_PAGE:
            raise ValueError("Full page missing pagination cursor; completeness uncertain")
        return rows, None
    cursor = meta["next_cursor"]
    if cursor is not None and (type(cursor) is not int or cursor < 1):
        raise ValueError("Invalid pagination cursor")
    return rows, cursor


def save_private(path: Path, raw: bytes) -> None:
    """Atomically publish new bytes, never overwrite a validated snapshot."""
    tmp = path.with_name(path.name + ".partial")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            raise FileExistsError(path)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--season", type=int, required=True, help="NBA season starting year")
    p.add_argument("--max-pages", type=int, default=16, help="Hard cap on TOTAL season pages")
    p.add_argument("--output-dir", type=Path, default=None)
    args = p.parse_args()
    if not 2000 <= args.season <= 2025 or not 1 <= args.max_pages <= 20:
        p.error("For this historical run: 2000 <= season <= 2025 and 1 <= max-pages <= 20")
    directory = args.output_dir or (Path.home() / "parallax-research" / "nba" / f"{args.season}-regular")
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)

    cursor = None
    seen_cursors: set[int] = set()
    seen_ids: set[int] = set()
    manifest = []
    statuses = Counter()
    key = None
    last_request_at = None
    complete = False

    for page_number in range(1, args.max_pages + 1):
        path = directory / f"page-{page_number:04d}.json"
        params = {"seasons[]": args.season, "season_type": "regular", "per_page": PER_PAGE}
        if cursor is not None:
            params["cursor"] = cursor
        url = API_URL + "?" + urlencode(params)
        if path.exists():
            raw = path.read_bytes()
            source = "cached"
        else:
            if key is None:
                key = getpass("BALLDONTLIE API key (never saved): ")
            if last_request_at is not None:
                time.sleep(max(0, SECONDS_BETWEEN_REQUESTS - (time.monotonic() - last_request_at)))
            last_request_at = time.monotonic()
            try:
                with urlopen(Request(url, headers={"Authorization": key}), timeout=25) as response:
                    raw = response.read()
            except (HTTPError, URLError) as exc:
                print(f"STOP: request failed on page {page_number}: {exc}; no retry")
                return 2
            source = "fetched"
        try:
            payload = json.loads(raw)
            games, next_cursor = validate_page(payload, args.season)
            ids = {g["id"] for g in games}
            if len(ids) != len(games) or seen_ids.intersection(ids):
                raise ValueError("Duplicate game ID within/across pages")
            if next_cursor == cursor or (next_cursor is not None and next_cursor in seen_cursors):
                raise ValueError("Repeated pagination cursor")
        except (ValueError, KeyError, TypeError) as exc:
            print(f"STOP: invalid {source} page {page_number}: {exc}")
            return 3
        if source == "fetched":
            try:
                save_private(path, raw)
            except (OSError, FileExistsError) as exc:
                print(f"STOP: cannot save page {page_number}: {exc}")
                return 4
        seen_ids.update(ids)
        statuses.update(str(g.get("status_state", g.get("status", "unknown"))) for g in games)
        manifest.append({
            "page": page_number, "file": path.name, "source_url": url,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "games": len(games), "next_cursor": next_cursor,
        })
        print(f"{source} page={page_number} games={len(games)} total={len(seen_ids)} next={next_cursor}", flush=True)
        if next_cursor is None:
            complete = True
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    result = {
        "sport": "NBA", "season": args.season,
        "query": {"season_type": "regular", "per_page": PER_PAGE},
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "complete_pagination": complete, "unique_games": len(seen_ids),
        "status_counts": dict(statuses), "pages": manifest,
        "research_only": True,
    }
    report = directory / "manifest.json"
    # Keep the manifest beside local snapshots; no credentials in contents.
    report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    os.chmod(report, 0o600)
    print(f"MANIFEST={report}")
    print(f"COMPLETE={complete} UNIQUE_GAMES={len(seen_ids)} STATUSES={dict(statuses)}")
    if not complete:
        print("STOP: page cap reached, historical season not yet complete")
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
