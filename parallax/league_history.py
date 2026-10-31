"""Load configured league results or the locally archived research snapshots."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from .league_elo_experimental import FinalGame, load_final_games_csv


def load_league_history(
    sport: str, configured_path: Path, *, explicit: bool,
) -> list[FinalGame]:
    """Prefer configured history, then use the bounded local research archive."""
    if explicit or configured_path.exists():
        rows = load_final_games_csv(configured_path)
        if not rows:
            raise ValueError(f"{sport} final-results history is empty")
        return rows
    research = Path.home() / "parallax-research"
    if sport == "NBA":
        rows = load_final_games_csv(research / "nba/nba-2024-2025-canonical.csv")
        if not rows:
            raise ValueError("NBA research final-results history is empty")
        return rows
    if sport != "NHL":
        raise ValueError("sport must be NBA or NHL")

    root = research / "nhl/historical"
    manifest = json.loads((root / "acquisition-manifest.json").read_text(encoding="utf-8"))
    rows: list[FinalGame] = []
    seen: set[str] = set()
    for season, info in sorted(manifest["seasons"].items()):
        for page in info["pages"]:
            raw = (root / page["filename"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != page["sha256"]:
                raise ValueError("NHL historical snapshot hash mismatch")
            document = json.loads(raw)
            games = document.get("data") if isinstance(document, dict) else None
            if not isinstance(games, list):
                raise ValueError("NHL historical snapshot malformed")  # noqa: TRY004
            for game in games:
                home, away = game["home_team"]["tricode"], game["away_team"]["tricode"]
                game_id = f"bdl:{game['id']}"
                if game_id in seen or game["home_score"] == game["away_score"]:
                    raise ValueError("NHL historical snapshot duplicate or tied game")
                seen.add(game_id)
                rows.append(FinalGame(
                    sport="NHL", game_id=game_id, season=int(season),
                    game_date=date.fromisoformat(game["game_date"]),
                    home_team=home, away_team=away,
                    winner_team=home if game["home_score"] > game["away_score"] else away,
                ))
    if len(rows) != 5248 or {game.season for game in rows} != {2022, 2023, 2024, 2025}:
        raise ValueError("NHL historical snapshot coverage mismatch")
    return rows
