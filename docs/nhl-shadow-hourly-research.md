# PARALLAX NHL bounded shadow job — opt-in research only

**NOT INSTALLED BY THIS COMMIT. DO NOT INSTALL ON THE PRODUCTION SWARMEDGE SERVICE PATH.**
The research checkout remains separate from the running NFL/CFB/MLB, intelligence and customer publisher releases.

- System LaunchDaemon label: `com.swarmaxis.parallax-nhl-shadow-research`
- Job template: `ops/launchd/com.swarmaxis.parallax-nhl-shadow-research.plist`
- Runner: `scripts.nhl_shadow_slate_research` (research-only)
- Every 7,200 seconds (2h); at most two fixture candidates, eight public GETs per scan; no retries.
- Snapshots: `~/parallax-research/nhl/forward/nhl-shadow-*.json`
- Logs: `~/parallax-research/nhl/shadow-hourly.{log,err}`

## Pre-install acceptance gates

1. Validate this feature branch in a **separate Git worktree**, not the checkout currently used by the live research daemon. Run offline tests:

   `PYTHONPATH=. ~/parallax-research/venv312/bin/python -m pytest -q tests/test_nhl_shadow_slate_research.py tests/test_nhl_shadow_unattended.py tests/test_nhl_shadow_rotation.py tests/test_league_shadow_evidence.py`

2. Run one bounded `--max-games 2 --write-research-snapshot` trial and inspect an archived JSON with capture time, pregame model, side economics and gates.
3. Inspect all absolute paths. The proposed system LaunchDaemon specifies UserName=scottsteele so the job can start after boot without a GUI login. Verify FileVault/unlock behavior, permissions, and actual reboot behavior separately before claiming full unattended recovery.
4. The system service is **already installed and active** with the original bounded pilot. **Do not reinstall or bootstrap it.** Promote source changes to its live checkout only after offline acceptance and a separate approval; coordinate with the job schedule and validate a subsequent run. Do not change the existing production services.
5. Monitor log size/rotation, daily request budget, and errors in the shadow-only archive.

## Enforced budget and dependency isolation

- Single-instance nonblocking lock protects against overlapping runs.
- Restart-persistent UTC-day budget: no more than 12 runs/day (at most 12 NHL schedule GETs and 84 Kalshi GETs/day from this pilot).
- A minimum 6,900-second inter-run interval blocks boot/relaunch storms.
- HTTP 403 or 429 triggers a 24-hour local cooldown; corrupted guard state fails closed.
- Jobs log concise summaries; full observations are in create-only JSON snapshots.
- Uses Python standard library, existing venv and historical SHA-verified local snapshots. No CFBD, BALLDONTLIE, ntfy or production publisher calls.
- We have **not** established an official NHL schedule API quota or reuse license; keep the research pilot conservative and review commercial-use permissions before any customer distribution.

## Remaining limitations

- The research job screens at most **two events every two hours**. In this feature branch, scheduling reads existing archived JSON snapshots and selects unseen upcoming fixtures first, otherwise the oldest screened events. It consumes no extra provider calls and retains the 8-call ceiling. Coverage is best-effort; late-listed or unlisted Kalshi markets may still be missed.
- Rejected contracts count as *attempted* for rotation; a future retry occurs after other events. The scheduler does not yet guarantee every game's market is screened near kickoff.
- Archival rotation uses the latest 1,000 snapshots within seven days and halts on malformed files instead of guessing.
- Historical Elo uses 2022–2025 seasons only; no 2026–27 completed results incorporated.
- Snapshots are prospective evidence for later forward-validation. **No automatic final-result reconciliation or validated holdout scoring exists yet.**
- No audited 2026 NHL fees, calibration, customer-feed publishing, ntfy alerts or orders.
- BUY is blocked for experimental forecasts; report flags buy_authorized=false, publish_authorized=false, orders_authorized=false.

Before publishing BUY to PARALLAX LIVE and ntfy, independently validate model calibration, fee policy, settlement identity, market economics, freshness, duplicate-outcome handling and reliable delivery.
