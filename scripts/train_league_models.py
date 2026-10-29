"""Fit deterministic NBA/NHL models from verified local finals snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parallax.league_elo_experimental import load_final_games_csv
from parallax.league_history import load_league_history
from parallax.league_model import train_artifact


def source_digest(sport: str, home: Path) -> str:
    root = home / "parallax-research"
    if sport == "NBA":
        return hashlib.sha256(
            (root / "nba/nba-2024-2025-canonical.csv").read_bytes()
        ).hexdigest()
    manifest_path = root / "nhl/historical/acquisition-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(manifest_path.read_bytes())
    for season, info in sorted(manifest["seasons"].items()):
        for page in info["pages"]:
            raw = (manifest_path.parent / page["filename"]).read_bytes()
            actual = hashlib.sha256(raw).hexdigest()
            if actual != page["sha256"]:
                raise ValueError("NHL source page hash mismatch")
            digest.update(f"{season}:{page['filename']}:{actual}\n".encode())
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--independent-history",
        type=Path,
        help="Independent provider CSV with the same verified finals",
    )
    parser.add_argument(
        "--source-authorization-reference",
        help="Document reference covering historical data use",
    )
    parser.add_argument(
        "--model-owner-approval-reference",
        help="Approved reference for the recorded model acceptance criteria",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summaries = {}
    for sport in ("NBA", "NHL"):
        history_path = Path("/nonexistent")
        games = load_league_history(sport, history_path, explicit=False)
        source_sha = source_digest(sport, Path.home())
        independent_sha = None
        if args.independent_history:
            independent_rows = [
                g
                for g in load_final_games_csv(args.independent_history)
                if g.sport == sport
            ]
            source_rows = [
                (
                    g.game_id,
                    g.season,
                    g.game_date,
                    g.home_team,
                    g.away_team,
                    g.winner_team,
                )
                for g in games
            ]
            independent_values = [
                (
                    g.game_id,
                    g.season,
                    g.game_date,
                    g.home_team,
                    g.away_team,
                    g.winner_team,
                )
                for g in independent_rows
            ]
            if source_rows != independent_values:
                raise ValueError(
                    f"{sport} independent results do not reconcile game-for-game"
                )
            independent_sha = hashlib.sha256(
                args.independent_history.read_bytes()
            ).hexdigest()
        artifact = train_artifact(
            games,
            sport=sport,
            source_sha256=source_sha,
            independent_source_sha256=independent_sha,
            source_authorization_reference=args.source_authorization_reference,
            model_owner_approval_reference=args.model_owner_approval_reference,
        )
        output = args.output_dir / f"{sport.lower()}_calibrated.json"
        output.write_text(
            json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        summaries[sport] = {
            "artifact": str(output),
            "artifact_sha256": artifact["artifact_sha256"],
            "certified": artifact["certified"],
            "gates": artifact["acceptance_gates"],
            "source_games": artifact["source_game_count"],
            "source_seasons": artifact["source_seasons"],
            "splits": artifact["splits"],
            "holdout_metrics": artifact["holdout_metrics"],
            "baseline_50_50": artifact["baseline_50_50"],
            "elo": artifact["elo"],
            "calibration": artifact["calibration"],
        }
    print(json.dumps(summaries, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
