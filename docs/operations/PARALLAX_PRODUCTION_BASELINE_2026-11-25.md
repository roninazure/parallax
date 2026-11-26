# PARALLAX Mac mini — production inventory and recovery record

**Evidence date:** 2026-11-25 (America/New_York), first CFB bridge scan completed 2026-11-26 02:14:10 UTC.  
**Scope:** roninazure/parallax Mac mini NFL + CFB only. No website or other sport deployments.  
**Evidence class:** operator-provided Mac mini CLI output, not an independently remote-inspected runtime.  
**Maintenance rule:** documentation updates do not authorize GitHub merges, launchd restarts, or deployments.

## Verified runtime inventory

| Lane | Launchd label | Release | Observation |
| --- | --- | --- | --- |
| NFL | `system/com.swarmedge.parallax-sports` | `46e87dfa7a7697488d14f19d83f4479031cde510` | `state = running`, PID `53088`, same pinned release during last pre-activation inspection. Activation script subsequently reported NFL still running on same release. |
| CFB recovery | `system/com.swarmedge.parallax-cfb-restore` | `a52d71f2f30c708782bd67bf8f7e3f8fee870923` | Independent hourly launchd bridge installed and first live scan passed at 2026-11-26T02:14:10.996606+00:00. |
| Original CFB scheduler lane | NFL service reads `PARALLAX_CFB_ENABLED` from `config/runtime.env` | NFL baseline | Switched off **after** first verified bridge scan to avoid duplicate CFB scans. |

**First bridge scan:** 34 scored positions, 12 publication-eligible BUY opportunities, 8 alerts sent, 4 deduplicated, local CFB feed published, health status PASS. This confirms the initial recovery, **not** continued hourly operation.

**Local paths on Mac mini:**
- Runtime base: `~/Library/Application Support/SwarmEdge`
- CFB health file: `state/parallax/cfb_recovery_bridge_health.json`
- CFB public feed: `state/parallax/public_feed/cfb.json`
- CFB phase diagnostics: `state/parallax/cfb_recovery_bridge_phase.json`
- Original configuration rollback backup: `ops/cfb-restore/runtime.env.before-cfb-bridge`
- Recovery stdout/stderr: `~/Library/Logs/SwarmEdge/parallax-cfb-restore.log` and `parallax-cfb-restore.err.log`

## Repository status and review boundaries

- `main` last code commit was `6a0061b2ed15a8457aad07c972dbd8521ee4395a` on 2026-10-06 02:11:54 UTC **before this documentation update**. It is **not** the production Mac mini release.
- PR #84 (`fix/cfb-reserve-kalshi-book-budget-20261122`) contains CFB fixes and bridge scripts. First operational deployment was achieved using staged candidate and pinned recovery scripts; **PR remains draft/unmerged** pending review and sustained operation checks.
- PR #85 (`fix/nfl-intelligence-not-buy-gated-20261122`) is a separate NFL intelligence candidate and **remains draft/unmerged**, because production integration and feed consumer validation have not been confirmed.
- Neither PR should be merged just to make `main` appear current. Diff footprint, target branch, and rollback mechanisms need review before any promotion.

## Non-disruptive recurring checks

Run from Mac mini terminal to inspect, without making changes:

```bash
sudo launchctl print system/com.swarmedge.parallax-sports |
  grep -E 'state =|pid =|PARALLAX_RELEASE_SHA'
sudo launchctl print system/com.swarmedge.parallax-cfb-restore |
  grep -E 'state =|last exit code|runs =|pid ='
BASE="$HOME/Library/Application Support/SwarmEdge"
cat "$BASE/state/parallax/cfb_recovery_bridge_health.json"
stat -f '%Sm %N' -t '%Y-%m-%dT%H:%M:%S%z' "$BASE/state/parallax/public_feed/cfb.json"
tail -n 30 "$HOME/Library/Logs/SwarmEdge/parallax-cfb-restore.err.log"
```

Verify the CFB health timestamp **advances** at a subsequent hourly interval. An idle launchd job may legitimately report `state = not running` between `StartInterval` invocations; inspect the job's last exit status and health timestamp instead. Confirm NFL release and PID independently.

## Recovery / rollback safety

The CFB bridge is a **temporary independent service**, installed to avoid restarting the known-working NFL service. Its activation script backs up `runtime.env` and only disables the original CFB scheduler setting after a successful bridge scan.

**Do not delete the backup or run legacy deployment `--apply` / broad launchd bootout/bootstrap scripts.** Previous NFL service bootstrap and rollback attempted to restart production and failed. Do not alter the running NFL job to perform CFB housekeeping.

If CFB health stops advancing, investigate logs and exit status first. Do not claim an automatic rollback or disable a service without reviewing the current launchd state; switching original CFB back on while the independent bridge is running could duplicate scans and notifications.

## Release criteria before closing PR #84

1. Two successive scheduled CFB scans with advancing timestamps, locally published feed, and no new notification errors.
2. NFL still running with same pinned release, with no overlap of independent and original CFB workers.
3. Separate dry-run review of rollback and persisted state paths.
4. Review minimal permanent source diff, CI results, and impact on NFL/CFB shared runtime and alerts.

This file records observed operation; it does not assert that PARALLAX LIVE website ingestion is functioning or that the latest GitHub `main` is deployed.
