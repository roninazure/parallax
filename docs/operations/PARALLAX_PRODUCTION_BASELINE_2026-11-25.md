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

## CFB deduplication release — operator-confirmed November 26, 2026

**Source:** Mac mini terminal results supplied by the operator on November 26 (America/New_York). These observations are not a fresh remote inspection and do not establish successful subsequent feed refresh or website ingestion.

### Problem and correction

Before the cutover, the independent CFB feed contained seven public BUY signals, including three pairs of equivalent Kalshi YES/NO contracts (Buffalo/Akron, Toledo/Ohio, and TCU/Texas Tech). Public BUY records did not include `selected_team`; the feed reported `runtime_state: UNKNOWN`, which alone did not prove the worker had failed.

The production publisher's `_eligible_cfb_buy_indexes` selected every eligible row without grouping by `economic_key`. The staging change instead chooses one eligible row per economic outcome, ranking by executable price, raw edge, liquidity, and stable identifiers. `_public_play` was changed to export verified `selected_team` for CFB eligible BUYs. An initial staging test exposed a missing `selected_team` entry in `PUBLIC_PLAY_FIELDS`; that allowlist was corrected before proceeding. The operator subsequently reported these targeted functional tests as passing:

- Equivalent Kalshi outcomes consolidated to one published index.
- Selected CFB team survived public export filtering.
- Candidate publisher loaded from the staged release.

**Limits of this test:** The checks used synthetic records and temporarily mocked CFB eligibility inside the test process. They did not prove the full scheduled scanner or public website consumer. The running CFB bridge, external market source, and NFL code were not modified as part of the patch.

### Cutover evidence

- Former release: `releases/a52d71f2f30c708782bd67bf8f7e3f8fee870923`
- Candidate release: `releases/cfb-kalshi-dedup-20261126`
- Independent launchd service: `system/com.swarmedge.parallax-cfb-restore`
- Rollback plist: `staging/cfb-restore-original.plist`
- CFB bridge script and runtime state path unchanged.

An initial cutover attempt stopped at the safety check because launchd reported an active scanner (PID 79106, run count 4, last exit code 0). The operator allowed that run to finish normally, then observed `CFB scanner idle — cutover window available`.

The second cutover command reported:

```text
PASS: CFB RELEASE SWITCHED
PASS: Original release preserved
PASS: Rollback plist preserved
PASS: launchd loaded new CFB release
NFL and website unchanged
```

These PASS messages verify that the launchd configuration switched to the candidate path and that backup materials were retained. They are **not** proof that the next scheduled scan successfully produced the corrected CFB JSON, nor that the Sites dashboard consumed it.

### Required acceptance / follow-up

1. At the next normal scheduled scan, inspect `state/parallax/cfb_recovery_bridge_health.json`, the feed's generation timestamp, and launchd's last exit code. Do not manually trigger another scan solely for this check; notifications could repeat.
2. For each public eligible CFB BUY, require a nonempty `selected_team`; for each `economic_key`, require at most one published BUY. If the normal slate has changed, do not hard-code four expected BUYs.
3. If checks fail, use the saved `staging/cfb-restore-original.plist` and the untouched `a52d71f2...` release as rollback evidence. Before any rollback, confirm the CFB scanner is idle and inspect live launchd state. Do not change NFL, re-enable the original shared CFB scheduler, or invoke a broad deployment script.
4. Reconcile PR #86 against the independently deployed CFB-only patch; do not merge its additional staging integration files or assume a public API exists without separate review.

**Operational status as of the last supplied terminal output:** CFB-only switch loaded, **awaiting fresh scheduled-scan validation**. NFL and website were intentionally out of scope.
