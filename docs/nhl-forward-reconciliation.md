# PARALLAX NHL forward validation — staged, not deployed

**NO production or research service changed.** The existing `com.swarmaxis.parallax-nhl-shadow-research` job continues every two hours with its existing eight-GET ceiling.

## Method

- Read immutable `~/parallax-research/nhl/forward/nhl-shadow-*.json` research-only pregame observations.
- Freeze the **earliest** eligible forecast for each independently matched official NHL game ID; do not retrospectively cherry-pick models or prices.
- After at least four hours following the independently recorded scheduled UTC start, fetch the official NHL daily scoreboard at `https://api-web.nhle.com/v1/score/YYYY-MM-DD` using **Eastern game date**.
- Require exact official game ID, original start time, regular-season code and season, date, both three-letter team IDs, final game state and a non-tied full-game score. Pending results are not losses.
- Create an exclusive, per-game result file `~/parallax-research/nhl/forward/reconciled/nhl-<id>.json`. Never overwrite pregame records or an existing reconciled game.
- Calculate prospective Brier and log loss from the frozen pregame home-win probability versus the official full-game winner. Do not call this calibrated or production-ready.

## Source and call limits

- Date-scoped NHL score endpoint supported by community-maintained API documentation. Verify actual JSON on the Mac mini after a game has finished, before activation.
- Runner `scripts.nhl_forward_reconcile.py --live`: at most **one additional NHL GET per UTC day**, zero Kalshi/BDL/CFBD/ntfy calls, no retries, 24h cooldown on 403/429, persistent daily quota and a nonblocking lock.
- Existing research scanner remains capped at 12 daily NHL schedule GETs; combined documented pilot maximum would be **13 NHL GETs/day** if both jobs run. This is not an official NHL quota and excludes other applications on the host.
- Test first with `--scoreboard-json` (offline, no provider calls). Do not install the separate daily job until offline tests, one real final-score verification, cross-process provider-limit review and explicit deployment acceptance.
- The proposed LaunchDaemon template is `ops/launchd/com.swarmaxis.parallax-nhl-forward-reconcile.plist`, once daily (`StartInterval=86400`), non-root account, read-only score API.

## Remaining blockers to production BUY

- Experimental Elo trained on 2022–2025 historical results; no current-season final results fed back into ratings yet.
- Need sufficient strictly forward-held NHL outcomes, calibration and economic evidence. A few first results cannot certify a signal.
- NHL fee schedule, original settlement terms, provider redistribution rights, freshness and duplicate-outcome handling remain subject to verification.
- No public customer feed, PARALLAX LIVE, ntfy, order/trade or autonomous publication path in this component.
