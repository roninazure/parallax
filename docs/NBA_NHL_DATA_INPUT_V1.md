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

## Verified vendor research: 2026-10-19 (development planning only)

Primary low-cost candidate for BOTH leagues: BALLDONTLIE.
- Terms (updated 2026-09-17): https://www.balldontlie.io/terms.html — Section 6 expressly permits lawful commercial analytics, cached data, derived/model outputs, subject to subscription and anti-competition/resale restrictions. Section 11 explicitly says the provider cannot clear all third-party intellectual-property/contractual rights. Do not represent this as unconditional league licensing or use it as the sole settlement authority.
- NBA API: https://docs.balldontlie.io/ — `GET https://api.balldontlie.io/v1/games?seasons[]=2025&season_type=regular&per_page=100`; cursor pagination, max 100/page; NBA Games listed FREE at 5 req/min.
- NHL API: https://nhl.balldontlie.io/ — `GET https://api.balldontlie.io/nhl/v1/games?seasons[]=2025&postseason=false&per_page=100`; NHL Games **not on free tier**; ALL-STAR $9.99/mo and 60 req/min. This is a commercial-terms candidate, NOT confirmation of full season coverage, upstream third-party rights or permission to activate a production lane. User-held authenticated account required.
- Baseline intended historical set: season starts 2022, 2023, 2024 for training/context, 2025 as holdout for each league, only after validating provider year-to-season semantics and completed regular-season coverage. Requests should be page-limited, serialized/cached, and stopped on 401/403/429 or any schema error; no automatic retry, no historical refetches from production.

### Ingestion gates discovered before first real snapshot

1. **NBA Cup regular-season inclusion:** NBA says Cup group games, quarterfinals and semifinals count as regular-season games; the Championship does not. The existing `nba_from_balldontlie` adapter excludes any truthy `ist_stage`, potentially omitting valid regular-season games. Do not silently modify it: first inspect licensed example rows and verify `season_type=regular` membership/field semantics; then add a focused regression test and minimal correction before importing 2025+.
2. **NHL BDL response schema:** It is different from the existing NHL *official club-schedule* adapter: `data[]`, `game_date`, `home_team.tricode`, `away_team.tricode`, `home_score`, `away_score`, `status_state`, `postseason`. An isolated BDL NHL adapter is required; do not mislabel BDL input as official NHL club JSON.
3. **NHL preseason ambiguity:** BDL `postseason=false` alone does not prove regular-season-only inclusion. Confirm actual historical sample coverage against independently verified league season totals and game identities before accepting any rows. Final full-game OT/SO scores only; no regulation-only labels.
4. **Coverage and identity:** Check duplicate IDs, abandoned/postponed rows, actual settled home/away winner, game date in the expected ET calendar, team relocation/abbreviation changes (especially Arizona/Utah), expected completed-game counts, missing cursor pages, and conflicting records. Quarantine anomalies rather than guessing.
5. **Model/market separation:** Team results alone can support an offline predictive calibration baseline, not a certified economic BUY. Archived pregame executable venue quotes, fees, spreads, liquidity and settlement rules require independent validation to estimate edge/profitability.

### Provider-key execution gate

No credentials have been supplied or stored here. Obtain a provider key through the owner's account, store only in the local shell/keychain (never chat or Git), retrieve ONE authorized page first and inspect fields/tier/rights/season membership before any bulk backfill. Save raw JSON with per-page SHA-256, source URL sans token, season, access timestamp, source terms version and pagination manifest. Reuse the existing normalization/backtest CLIs once the adapter schema and season filters are proven. No network acquisition or actual holdout results are claimed by this documentation.
