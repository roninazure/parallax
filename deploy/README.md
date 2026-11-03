# PARALLAX deployment contract

This directory contains versioned deployment inputs for the production PARALLAX sports runtime.

Nothing in this directory is activated merely by being present in a Git branch. Production cutovers are explicit, SHA-addressed, validated, and reversible.

## Production owner

`launchd/com.swarmedge.parallax-sports.plist.in` is the production owner for the unattended sports scheduler.

The scheduler entrypoint is:

```text
scripts/parallax_unattended.py
```

It owns the NFL, optional CFB, MLB, and prospective-reconciliation schedules. Each lane keeps its own business logic; the unattended scheduler owns only cadence, isolation, locking, health state, and child-process execution.

The optional `nfl_sportsbook_research` lane is disabled unless `PARALLAX_NFL_SPORTSBOOK_RESEARCH_ENABLED=1` is set in the runtime environment. When enabled, it performs at most one NFL research capture per hour, persists its attempt throttle under external state, first checks the actual account quota through the zero-credit `GET /v4/sports/` endpoint, and writes research snapshots outside the public-feed directory. It is not a customer publisher and never sets BUY publication eligibility.


## Optional NFL sportsbook research capture

Enable only after the operator confirms the provider plan and quota budget:

```text
PARALLAX_NFL_SPORTSBOOK_RESEARCH_ENABLED=1
# Key remains in the existing macOS Keychain service swarmaxis-the-odds-api.
# THE_ODDS_API_KEY is supported only as an explicit protected environment fallback.
```

The feature is disabled by default. The scheduler delays its first attempt for one hour after startup or after a runtime transition from disabled to enabled, then invokes the capture lane no more than hourly. The capture script independently enforces a persistent minimum 3,600-second interval across restarts and failed requests. It reserves the paid odds attempt before making that request, makes no retries, and requires at least 10 credits of headroom. Before quota checks or any Odds API HTTP, it filters the official schedule to games 60 minutes to 24 hours before kickoff; if none qualify, it records a clean skip. When games qualify, it retrieves the account's real usage from the provider's documented **zero-credit** `GET /v4/sports/` endpoint and rejects missing, corrupt, charged, or insufficient-quota responses without attempting paid odds. Its quota receipt is atomically stored under external `research/nfl_sportsbook/quota_state.json`, independent of the legacy paper-only `scripts/sports_scan.py` state; the production MLB command (`python -m parallax scan`) does not maintain that legacy file. The research script retrieves the existing Odds API credential from macOS Keychain service `swarmaxis-the-odds-api` for the launchd process user, or uses an explicitly provided protected `THE_ODDS_API_KEY` environment value. No key is logged or copied into the research snapshot. **Keychain visibility from the unattended launchd user and actual account subscription/budget remain attended pre-deployment gates.** The resulting NFL research odds call is separate from existing MLB/CFB provider paths, whose schedules and behavior remain unchanged.

Snapshots and capture state are written beneath the external state directory at `research/nfl_sportsbook/`; they are not exported to customer feeds. Every snapshot is hard-coded `research_only=true` and `buy_publication_eligible=false`. No model, scanner, notification, or customer BUY behavior is changed.

## Release layout

Production releases are immutable and named by full Git SHA:

```text
~/Library/Application Support/SwarmEdge/
  releases/<git-sha>/
  venvs/<git-sha>/
  state/
  logs/
```

Do not deploy directly from a mutable checkout.

## Package a release

Create a release from a committed SHA with the existing migration/release tooling:

```sh
python scripts/migration_preflight.py create-release \
  --source "$SOURCE" \
  --sha "$SHA" \
  --target-release "$RELEASE"
```

The release directory must remain immutable after packaging.

## Render the launchd plist

Render `launchd/com.swarmedge.parallax-sports.plist.in` with absolute paths for:

- release root
- SHA-matched venv Python
- runtime environment file
- PARALLAX state directory
- log directory
- release SHA

Validate the rendered plist before any cutover:

```sh
plutil -lint "$RENDERED_PLIST"
```

## Generate the deployment manifest

Bind the release, dependency lock, runtime environment, launchd plist, and scheduler entrypoint:

```sh
# RELEASE is already read-only after create-release. Never write into it.
# RUNTIME_ROOT, SHA, RELEASE, RUNTIME_ENV and RENDERED_PLIST are reviewed inputs.
VENV_PYTHON="$RUNTIME_ROOT/venvs/$SHA/bin/python3"
CANDIDATE_MANIFEST="$RUNTIME_ROOT/run/candidates/$SHA.manifest.json"

"$VENV_PYTHON" scripts/deployment_manifest.py generate \
  --root "$RELEASE" \
  --runtime-env "$RUNTIME_ENV" \
  --plist "$RENDERED_PLIST" \
  --lock "$RELEASE/requirements.lock" \
  --entrypoint "$RELEASE/scripts/parallax_unattended.py" \
  --output "$CANDIDATE_MANIFEST"

"$VENV_PYTHON" scripts/deployment_manifest.py validate \
  --root "$RELEASE" \
  --runtime-env "$RUNTIME_ENV" \
  --plist "$RENDERED_PLIST" \
  --lock "$RELEASE/requirements.lock" \
  --entrypoint "$RELEASE/scripts/parallax_unattended.py" \
  --manifest "$CANDIDATE_MANIFEST"
```

The candidate manifest is staged **outside** the immutable release and must never overwrite the active `run/parallax-sports-manifest.json` during inspection or rehearsal. Generate it using the SHA-matched venv interpreter so `python_version` describes the candidate runtime, not the operator's shell. The manifest records the Git SHA, Python version, and hashes needed to detect deployment drift. Revalidate it against the exact loaded plist and runtime inputs before a separately approved cutover; preserve the previous manifest with the saved plist for rollback.

## Cutover rules

Before reloading `com.swarmedge.parallax-sports`:

1. verify the target release SHA
2. verify the SHA-matched venv exists
3. verify the rendered plist with `plutil -lint`
4. preserve the current plist as the immediate rollback
5. reload the launchd job
6. verify `state = running`
7. verify the running program path points at the target venv
8. verify `PARALLAX_RELEASE_SHA` matches the target SHA
9. verify a fresh heartbeat, a valid scan output, and no critical errors. `RUNNING` and `DEGRADED` are acceptable when the active process, SHA, and heartbeat are correct; documented partial provider coverage is a successful scan and must not trigger rollback by itself. `FAILED`, malformed scan output, failed acquisition/processing, or stale heartbeat is a rollback condition.
10. preserve accurate last-success timestamps: partial scans advance them; failed scans do not.

Do not remove the previous known-good release until the new release is proven stable.

## Operational safety

- launchd is the process owner
- one sports lane failure must not prevent later lanes from running
- provider rate limits are stop conditions, not retry loops
- runtime state lives outside immutable release directories
- alert failures must not crash sports scans
- production changes must preserve rollback

## Scanner correction cutover and automatic rollback

Stage the target under `releases/<full-sha>` and render a candidate plist before deployment. Keep the current plist byte-for-byte in a timestamped backup and record the currently loaded launchd job definition. The production cutover, when separately authorized, must install the candidate plist, bootstrap/reload `com.swarmedge.parallax-sports`, then poll health for at most 90 seconds. Accept only when the launchd process is active, its program path and `release_sha` match the target, heartbeat age is at most 30 seconds, scan output validates, and `recent_errors` contains no critical error. `RUNNING` or `DEGRADED` passes; partial coverage diagnostics remain visible and do not fail acceptance. On timeout or any failed predicate, restore the saved plist and reload the previous job automatically, then verify its process and SHA. Do not change or reload the separate customer publisher. This procedure does not authorize a production cutover.
