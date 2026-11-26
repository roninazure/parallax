"""Retrospective Elo vs archived NFL moneylines; never a live BUY or P&L test.

Historic nflverse moneylines have no per-quote observed_at timestamp.
Do NOT interpret these values as available at a historical PARALLAX BUY time.
No production state, secrets, or trading interfaces are accessed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from urllib.request import Request, urlopen

from parallax.nfl import SOURCE_URL, NFLGame, _advance, _int, _kickoff, _probability, parse_games
from sports.devig import american_to_decimal, devig_two_way

MAX_SOURCE_BYTES = 8_000_000
REQUIRED = {"game_id", "season", "game_type", "gameday", "gametime",
            "home_team", "away_team", "home_score", "away_score",
            "home_moneyline", "away_moneyline"}


def _market_home_probability(raw: dict[str, str]) -> float | None:
    try:
        home, away = (float(raw.get(key) or "") for key in ("home_moneyline", "away_moneyline"))
        if not all(math.isfinite(x) and abs(x) >= 100 for x in (home, away)):
            return None
        p, _ = devig_two_way(american_to_decimal(home), american_to_decimal(away))
        return p if math.isfinite(p) and 0 < p < 1 else None
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        return None


def _metrics(rows: list[dict], key: str) -> dict:
    if not rows:
        return {"count": 0, "brier": None, "log_loss": None,
                "mean_probability": None, "observed_home_win_rate": None}
    probs = [row[key] for row in rows]
    y = [row["home_win"] for row in rows]
    return {
        "count": len(rows),
        "brier": round(mean((p - outcome) ** 2 for p, outcome in zip(probs, y)), 8),
        "log_loss": round(mean(-(outcome * math.log(max(p, 1e-15))
                                 + (1 - outcome) * math.log(max(1 - p, 1e-15)))
                               for p, outcome in zip(probs, y)), 8),
        "mean_probability": round(mean(probs), 6),
        "observed_home_win_rate": round(mean(y), 6),
    }


def benchmark(csv_text: str, *, season: int = 2025, min_paired: int = 100,
              variant: str = "baseline") -> dict:
    reader = csv.DictReader(io.StringIO(csv_text))
    columns = set(reader.fieldnames or ())
    if not REQUIRED.issubset(columns):
        raise ValueError("missing required historical columns: " + ", ".join(sorted(REQUIRED - columns)))
    raw_by_id = {row["game_id"]: row for row in reader}
    if variant == "baseline":
        games = [game for game in parse_games(csv_text) if game.season <= season]
    elif variant == "postseason_and_tie_corrected":
        # EXPERIMENTAL ONLY: recover nflverse's actual WC/DIV/CON/SB types
        # rather than treating them as 'POST', and handle ties as 0.5 outcomes.
        # Evaluate identical regular-season games; this candidate NEVER
        # flows into NFL live prediction or publication.
        games = []
        for raw in raw_by_id.values():
            game_type = str(raw.get("game_type") or "").upper()
            year = _int(raw.get("season"))
            if year is None or year > season or game_type not in {"REG", "POST", "WC", "DIV", "CON", "SB"}:
                continue
            kickoff = _kickoff(raw)
            home, away = str(raw.get("home_team") or "").strip(), str(raw.get("away_team") or "").strip()
            if not kickoff or not home or not away:
                continue
            games.append(NFLGame(raw["game_id"], year, game_type, kickoff, home, away,
                                 _int(raw.get("home_score")), _int(raw.get("away_score"))))
        games.sort(key=lambda game: (game.kickoff, game.game_id))
    else:
        raise ValueError("unknown forecast variant")
    ratings: dict[str, float] = {}
    scored: list[dict] = []
    index = 0
    while index < len(games):
        kickoff = games[index].kickoff
        end = index
        while end < len(games) and games[end].kickoff == kickoff:
            end += 1
        # Compute all simultaneous forecasts before processing ANY of their results.
        scheduled = [(game, _probability(ratings.get(game.home_team, 1500.0),
                                         ratings.get(game.away_team, 1500.0)))
                     for game in games[index:end]]
        for game, p_home in scheduled:
            if game.season != season or game.game_type != "REG" or game.home_score is None or game.away_score is None:
                continue
            if game.home_score == game.away_score:
                continue  # binary winner comparison cannot label ties as away wins
            matched_p = _market_home_probability(raw_by_id.get(game.game_id, {}))
            scored.append({"game_id": game.game_id, "kickoff": game.kickoff,
                           "home_team": game.home_team, "away_team": game.away_team,
                           "home_win": int(game.home_score > game.away_score),
                           "elo_home": p_home, "market_home": matched_p})
        for game, p_home in scheduled:
            if (variant == "postseason_and_tie_corrected"
                    and game.home_score is not None and game.away_score is not None
                    and game.home_score == game.away_score):
                delta = 20.0 * (0.5 - p_home)
                ratings[game.home_team] = ratings.get(game.home_team, 1500.0) + delta
                ratings[game.away_team] = ratings.get(game.away_team, 1500.0) - delta
            else:
                _advance(ratings, game, p_home)  # existing production Elo for baseline
        index = end

    matched = [row for row in scored if row["market_home"] is not None]
    elo, market = _metrics(matched, "elo_home"), _metrics(matched, "market_home")
    segments = {}
    for label, rows in {
        "elo_home_underdog_lt_40pct": [r for r in matched if r["elo_home"] < .4],
        "elo_home_favorite_ge_60pct": [r for r in matched if r["elo_home"] >= .6],
        "market_home_underdog_lt_40pct": [r for r in matched if r["market_home"] < .4],
        "disagreement_at_least_10pp": [r for r in matched if abs(r["elo_home"] - r["market_home"]) >= .10],
    }.items():
        segments[label] = {"elo": _metrics(rows, "elo_home"),
                           "market": _metrics(rows, "market_home")}
    if len(matched) < min_paired:
        status = "INSUFFICIENT_PAIRED_ODDS"
    elif elo["brier"] >= market["brier"] or elo["log_loss"] >= market["log_loss"]:
        status = "ELO_BEHIND_MARKET"
    else:
        status = "ELO_AHEAD_IN_RETROSPECTIVE_SAMPLE_ONLY"

    return {
        "season": season, "model_version": "nfl-v1-elo-rolling", "variant": variant,
        "source_url": SOURCE_URL, "source_sha256": hashlib.sha256(csv_text.encode()).hexdigest(),
        "status": status, "completed_binary_games": len(scored),
        "paired_market_games": len(matched), "unpaired_games": len(scored) - len(matched),
        "minimum_paired_games": min_paired,
        "paired_comparison": {"elo": elo, "market_devig": market,
                              "brier_skill_vs_market": round(1 - elo["brier"] / market["brier"], 6)
                              if matched and market["brier"] else None},
        "segments": segments,
        "limits": [
            "Archived sportsbook quotes have no observation timestamp: NOT an as-of executable edge backtest.",
            "Scores advance ratings after each kickoff group's predictions; target outcomes never enter their own predictions.",
            "Same matched games for Elo and market metrics; ties excluded from binary scoring.",
            "No weather, starting-QB, injury, news or point-in-time bookmaker movement incorporated.",
            "No claim of profitable BUYs, publishable fair value, or production certification.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", type=Path, help="frozen local nflverse games.csv")
    src.add_argument("--fetch-once", action="store_true", help="one bounded public nflverse request; no retries")
    parser.add_argument("--season", type=int, default=2025)
    parser.add_argument("--variant", choices=("baseline", "postseason_and_tie_corrected"), default="baseline")
    parser.add_argument("--min-paired", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.fetch_once:
        request = Request(SOURCE_URL, headers={"User-Agent": "PARALLAX-offline-market-benchmark/1"})
        with urlopen(request, timeout=25) as response:
            data = response.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError("historical source exceeds maximum bytes")
        csv_text = data.decode("utf-8")
    else:
        csv_text = args.csv.read_text(encoding="utf-8")
    result = benchmark(csv_text, season=args.season, min_paired=args.min_paired, variant=args.variant)
    result["generated_utc"] = datetime.now(timezone.utc).isoformat()
    output = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output)
    return 2 if result["status"] == "INSUFFICIENT_PAIRED_ODDS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
