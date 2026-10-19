# NBA/NHL: source snapshots for offline research

Status: **No real historical dataset acquired or validated yet.** No production permissions, market API calls, or BUY publication.

Research-only files in `feature/nba-nhl-shadow-20261019` accept **locally saved JSON** and convert verified final game results into a canonical CSV for the existing backtest.

## Data-source research, 2026-10-19

NBA: BALLDONTLIE `GET https://api.balldontlie.io/v1/games` documents season filtering (`seasons[]=2025`), `season_type=regular`, final-game status, cursor pagination, and up to 100 records per page. It requires a user-held API key; free tier lists five calls/minute. Do not store keys in Git or share them in chat. Commercial use/derived output rights must still be checked directly with the provider.

NHL: The NHL web interface has `/v1/club-schedule-season/{team}/{YYYYYYYYY}` and final records. The NHL's web terms state noncommercial/personal-use restrictions. **Do not auto-fetch, mirror, or include NHL-sourced content in a commercial build without a license/permission review.** Alternative: a provider licensing data for analytics-derived outputs (e.g. SportsDataIO commercial subscription, quote required).

Separate sport snapshots must use full-game **including NHL OT/SO** winner and exclude preseason, playoffs and regulation-only settlement contracts.

## Pipeline: zero network operations

From repository root, once independently sourced and appropriately licensed JSON snapshots exist:

```bash
PYTHONPATH=. python3 scripts/nba_nhl_normalize_snapshots.py \
  --nba-json /safe/staging/BDL_games_regular_2025_page1.json \
  --nba-regular-query-verified \
  --output /safe/staging/nba_nhl_verified_finals.csv

PYTHONPATH=. python3 scripts/nba_nhl_offline_backtest.py \
  /safe/staging/nba_nhl_verified_finals.csv --holdout-season 2025
```

Use `--nhl-json` repeatedly to include separately authorized and saved local NHL club-season snapshots. The parser de-duplicates both team perspectives by official game ID and rejects disagreements. `--nba-json` may be repeated for all cursor pages. Keep a local manifest of source, date of access, exact request/filter, license/attribution, seasons covered and raw-file checksum. **The flag indicates a documented check of the originating NBA query, not independent licensing clearance.**

The Elo baseline has **no calibrated parameters**, no independent contract orientation verification, no rest/roster/goalie inputs, and no proven market edge. This pipeline is for research only. Never interpret synthetic test pass counts as predictive accuracy.
