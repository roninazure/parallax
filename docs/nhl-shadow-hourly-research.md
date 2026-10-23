# PARALLAX NHL hourly shadow job — opt-in research only

**NOT INSTALLED BY THIS COMMIT. DO NOT INSTALL ON THE PRODUCTION SWARMEDGE SERVICE PATH.**
The research checkout remains separate from the running NFL/CFB/MLB, intelligence and customer publisher releases.

- System LaunchDaemon label: `com.swarmaxis.parallax-nhl-shadow-research`
- Job template: `ops/launchd/com.swarmaxis.parallax-nhl-shadow-research.plist`
- Runner: `scripts.nhl_shadow_slate_research` (research-only)
- Every 3,600 seconds; at most two fixture candidates, eight public GETs; no retries.
- Snapshots: `~/parallax-research/nhl/forward/nhl-shadow-*.json`
- Logs: `~/parallax-research/nhl/shadow-hourly.{log,err}`

## Pre-install acceptance gates

1. Fast-forward the research branch and run offline tests:

   `PYTHONPATH=. ~/parallax-research/venv312/bin/python -m pytest -q tests/test_nhl_shadow_slate_research.py tests/test_nhl_shadow_unattended.py tests/test_league_shadow_evidence.py`

2. Run one bounded `--max-games 2 --write-research-snapshot` trial and inspect an archived JSON with capture time, pregame model, side economics and gates.
3. Inspect all absolute paths. The proposed system LaunchDaemon specifies UserName=scottsteele so the job can start after boot without a GUI login. Verify FileVault/unlock behavior, permissions, and actual reboot behavior separately before claiming full unattended recovery.
4. Only after explicit operational approval, install this as a root-owned system plist in /Library/LaunchDaemons with mode 0644, and bootstrap its unique system label. The worker runs as scottsteele, not root. No existing production service definition should change.
5. Monitor log size/rotation, daily request budget, and errors in the shadow-only archive.

## Remaining limitations

- This is a **two-game-per-hour pilot**, not full-slate coverage. Oldest-first selection may repeatedly sample the same fixtures; add fair slate rotation / deduplication before claiming complete game coverage.
- Historical Elo uses 2022–2025 seasons only; no 2026–27 completed results incorporated.
- Snapshots are prospective evidence for later forward-validation. **No automatic final-result reconciliation or validated holdout scoring exists yet.**
- No audited 2026 NHL fees, calibration, customer-feed publishing, ntfy alerts or orders.
- BUY is blocked for experimental forecasts; report flags buy_authorized=false, publish_authorized=false, orders_authorized=false.

Before publishing BUY to PARALLAX LIVE and ntfy, independently validate model calibration, fee policy, settlement identity, market economics, freshness, duplicate-outcome handling and reliable delivery.
