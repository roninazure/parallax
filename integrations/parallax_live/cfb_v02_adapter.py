#!/usr/bin/env python3
"""Read-only CFB public-feed to PARALLAX LIVE v0.2 supplement adapter.

Does not publish externally. Never connects to production state/credentials.
Consumers must merge ONLY the 'cfb' lane and CFB plays into a separately
verified current site feed; never overwrite NFL, MLB, or a current live feed.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

MAX_AGE = timedelta(minutes=30)
VENUES = {"KALSHI": "KALSHI", "PMUS": "POLYMARKET"}


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("Missing feed timestamp")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid feed timestamp") from exc
    if dt.tzinfo is None:
        raise ValueError("Timezone required")
    return dt.astimezone(UTC)


def bounded(value, lower, upper):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid numeric field")
    result = float(value)
    if not math.isfinite(result) or not lower <= result <= upper:
        raise ValueError("Out-of-range numeric field")
    return result


def convert_cfb_feed(feed, *, now=None):
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if feed.get("read_only") is not True or feed.get("lane") != "CFB":
        raise ValueError("Expected sanitized read-only CFB feed")
    if feed.get("runtime_state") != "LIVE":
        raise ValueError("CFB feed is not verified LIVE")
    generated_at = parse_time(feed.get("generated_at"))
    if not -timedelta(minutes=1) <= current - generated_at <= MAX_AGE:
        raise ValueError("CFB feed stale or future-dated")
    if feed.get("source_as_of") is None:
        raise ValueError("CFB source freshness unavailable")
    if not -timedelta(minutes=1) <= current - parse_time(feed["source_as_of"]) <= MAX_AGE:
        raise ValueError("CFB source data stale")
    if not isinstance(feed.get("plays"), list):
        raise ValueError("CFB plays missing")
    output = []
    for row in feed["plays"]:
        if not isinstance(row, dict) or row.get("action") != "BUY":
            continue
        if row.get("publication_eligible") is not True or row.get("sport") != "CFB":
            continue
        if row.get("freshness") != "FRESH" or row.get("status") != "CURRENT":
            continue
        if row.get("venue") not in VENUES:
            continue
        team = row.get("selected_team")
        if not isinstance(team, str) or not team.strip() or len(team) > 80:
            continue  # A ticker cannot establish the selected team.
        try:
            updated = parse_time(row.get("updated_at"))
            expires = parse_time(row.get("expires_at"))
            resolution = parse_time(row.get("resolution_time"))
            if not (updated <= current < expires and current < resolution):
                continue
            if current - updated > MAX_AGE:
                continue
            price = bounded(row.get("price"), 0.00001, 0.99999)
            probability = bounded(row.get("model_probability"), 0, 1)
            edge = bounded(row.get("edge_pp"), -100, 100)
        except ValueError:
            continue
        key = row.get("signal_id")
        if not isinstance(key, str) or not key.startswith("px1_"):
            continue
        output.append({
            "id": key, "verdict": "BUY", "sport": "CFB",
            "selection": team.strip(), "venue": VENUES[row["venue"]],
            "price_cents": round(price * 100, 2),
            "model_probability": probability, "edge_pp": edge,
            "rationale": "Certified CFB BUY from a read-only production public feed.",
        })
    output.sort(key=lambda r: (-r["edge_pp"], r["id"]))
    return {
        "contract_version": "1.0",
        "snapshot_id": "px-cfb-" + hashlib.sha256((feed["generated_at"] + "|" + str(len(output))).encode()).hexdigest()[:16],
        "mode": "live", "generated_at": generated_at.isoformat(),
        "read_only": True, "execution_enabled": False, "source_state": "LIVE",
        "data_quality": {
            "verified_fields_only": True, "schedule_metadata_verified": False,
            "policy": "CFB-only certified BUYs; stale or ambiguous positions withheld.",
        },
        "lanes": [{"id": "cfb", "label": "CFB", "state": "ACTIVE"}],
        "plays": output,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="sanitized public_feed/cfb.json")
    parser.add_argument("--output", type=Path, required=True, help="staging JSON; NOT a network upload")
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        raise SystemExit("Refusing to overwrite source")
    result = convert_cfb_feed(json.loads(args.source.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_name(args.output.name + ".tmp")
    tmp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    tmp.replace(args.output)
    print(f"STAGED CFB v0.2 supplement: {len(result['plays'])} certified BUYs")


if __name__ == "__main__":
    main()
