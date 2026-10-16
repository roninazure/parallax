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
python scripts/deployment_manifest.py generate \
  --root "$RELEASE" \
  --runtime-env "$RUNTIME_ENV" \
  --plist "$RENDERED_PLIST" \
  --lock "$RELEASE/requirements.lock" \
  --entrypoint "$RELEASE/scripts/parallax_unattended.py" \
  --output "$RELEASE/deploy/manifest.json"
```

The manifest records the Git SHA, Python version, and hashes needed to detect deployment drift.

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
