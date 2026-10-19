# PARALLAX NBA + NHL — Isolated Shadow Research v0 (October 19, 2026)

**Status:** Offline research only. Not certified for a production BUY, not deployed, and not connected to any unattended job.

## Explicit boundaries

- Scope: pregame **full-game winner** only, NBA regular season and NHL regular season.
- NHL full-game winner includes overtime and shootout, **not** regulation-only three-way markets.
- Polymarket US and Kalshi are future venue targets, not data sources for fair probability.
- No changes to `scripts/parallax_unattended.py`, `parallax/public_feed.py`, existing sport providers, ntfy, customer dashboards, or the Mac mini.
- No provider requests, automatic trading, market publication, or customer signals.
- Never certify a play using unvalidated or synthetic probabilities.

## Actual repository seams (main at `6a0061b2ed15a8457aad07c972dbd8521ee4395a`)

| Layer | Existing implementation | Future NBA/NHL integration |
| --- | --- | --- |
| Authoritative sport fixture identity | `parallax/nfl.py`, `parallax/mlb.py`, `parallax/cfb.py` | Add sport-specific schedule readers and exact home/away/team/date matching; fail closed on ambiguity |
| Venue acquisition / bounds | `parallax/sources.py`, `parallax/pmus_acquisition.py`, `parallax/pmus_market_data.py` | Share budgeted clients, discovery ceilings, cooldowns; do **not** poll venue per game |
| Model evidence | `parallax/models.py` `Evidence`, `parallax/engine.py` `qualify` | Each sport needs independent holdout validation + sport-specific safety/certification policy |
| Supervisor | `scripts/parallax_unattended.py` | No hook until the release gate is approved; stagger scans so NFL fast refresh keeps priority |
| Customer feeds | `parallax/public_feed.py` (`PUBLIC_LANES`) | Explicit sport whitelist and fail-closed proof required before feed registration |
| ntfy | Existing dispatch | Existing provider and dedupe retained; no new alert service |

## Data-source feasibility

1. NBA: BALLDONTLIE documents `games` as free (5 requests/minute), but player injuries and advanced stats require paid tiers. Commercial redistribution, attribution and SLA must be reviewed separately. No account or credentials accessed.
2. NHL: Kalshi lists full-game winner contracts under `KXNHLGAME`. Historic data-provider and commercial-rights decision still pending; do not claim NHL official feed access exists without verification.
3. Sportsbook and prediction-market prices are not independent model evidence; bookmaker odds history, if lawfully acquired, can be used for **comparison**, not model feature leakage.
4. Cached, canonical historical final-result files are required for offline replay; source, capture date, season coverage, and licensing must be documented.

## Prototype now available

- `parallax/league_elo_experimental.py`: pure Python, separate NBA/NHL Elo states; game-date chronology; no API dependencies; unknown team default and no market inputs.
- `scripts/nba_nhl_offline_backtest.py`: CLI producing Brier/log loss/ECE for a held-out starting season.
- `tests/test_league_elo_experimental.py`: synthetic offline tests for separate ratings, same-date leakage, future-result exclusion, fail-closed identity and reporting.

Example, from repo root **only after a verified local historical snapshot exists**:

```bash
PYTHONPATH=. python3 scripts/nba_nhl_offline_backtest.py /safe/local/verified-nba-nhl-finals.csv --holdout-season 2025
```

CSV header: `sport,game_id,season,game_date,home_team,away_team,winner_team`.

- `game_date` is the official fixture date in US Eastern calendar.
- `season` is the starting year of the season; e.g. `2025` for 2025–26.
- `winner_team` is the officially settled **full-game** winner, not regulation-only.
- Only authenticated/licensed source **final** regular-season game results belong in the file.
- Prototype has deliberately provisional Elo home adjustments / K factors. It is **not calibrated**, has no verified fixtures, does not account for rest or injuries, and cannot authorize a BUY.

## Release gates, in order

1. Choose and document a legally usable historical + current NBA and NHL fixture/results feed, with expected calls and storage costs.
2. Verify sport/venue contract rules, team identities, regular-season scoping, and explicit YES/NO orientation, using sanitized real examples and offline fixtures.
3. Build chronological historical snapshots, run multi-season holdouts and ECE / Brier / logloss diagnostics; compare to a baseline and actual archived executable quotes when available.
4. Independently validate availability/injuries (NBA) and goalie/rest effects (NHL); avoid false claims of model accuracy.
5. Offline end-to-end shadow tests of mapping, cold-start behavior, failures, 429 no-retry, liquidity, fees, stale quotes, dedupe, and logging. No public feed or alerts yet.
6. Inspect Mac mini peak CPU, memory, available storage, and provider quotas during representative simultaneous sports activity; preserve CFB one-`/games`-per-hour ceiling.
7. Stage and approve separate production activation with rollback. No NBA/NHL certified BUY, visible commercial claim, or alert until this evidence exists.

## Current production observations — *out of scope*

- Last read-only Mac mini sample: M1, 16 GB, 80–94% idle, zero swap, 26 GiB writable data-volume free.
- Existing NFL market coverage occasionally reports `DEGRADED` because Kalshi lower-priority markets rotate. Recent CFB/NFL child exceptions have truncated root causes.
- These warrant separate production incident review, **not** incidental edits in this NBA/NHL branch.
