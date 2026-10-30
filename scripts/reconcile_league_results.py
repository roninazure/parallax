"""Read-only, league-scoped reconciliation of two local NBA/NHL finals CSVs.

Agreement is not proof that the files came from independent providers, that
their use is licensed, or that a model is certified. No network or runtime IO.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parallax.league_elo_experimental import load_final_games_csv
from parallax.league_model_v2 import reconcile_independent_history


def reconcile_files(*, primary: Path, secondary: Path, sport: str) -> dict[str, object]:
    """Compare fixture identity and winners without trusting provider game IDs."""
    if sport not in {"NBA", "NHL"}:
        raise ValueError("sport must be NBA or NHL")
    primary_sha = hashlib.sha256(primary.read_bytes()).hexdigest()
    secondary_sha = hashlib.sha256(secondary.read_bytes()).hexdigest()
    if primary_sha == secondary_sha:
        raise ValueError("primary and secondary source files have identical SHA-256")
    primary_games = [g for g in load_final_games_csv(primary) if g.sport == sport]
    secondary_games = [g for g in load_final_games_csv(secondary) if g.sport == sport]
    if not primary_games or not secondary_games:
        raise ValueError(f"{sport} results missing from one or both source files")
    fingerprint = reconcile_independent_history(
        primary_games, secondary_games, sport=sport
    )
    return {
        "sport": sport,
        "status": "FIXTURES_RECONCILED",
        "games_reconciled": len(primary_games),
        "primary_source_sha256": primary_sha,
        "secondary_source_sha256": secondary_sha,
        "canonical_history_sha256": fingerprint,
        "provider_independence_verified": False,
        "source_authorization_verified": False,
        "forward_holdout_verified": False,
        "model_certified": False,
        "production_eligible": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", choices=("NBA", "NHL"), required=True)
    parser.add_argument("--primary-history", type=Path, required=True)
    parser.add_argument("--independent-history", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = reconcile_files(
            primary=args.primary_history,
            secondary=args.independent_history,
            sport=args.sport,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"RECONCILIATION_FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
