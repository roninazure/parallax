# Isolated NBA/NHL evidence collector

This is a separate, read-only user LaunchAgent. It never loads or reloads
`com.swarmedge.parallax-sports`. Run from a SHA-pinned release directory, with
dedicated evidence and state directories. Never point those directories at the
production state or feeds.

## Read-only preflight

From the Mac mini user session, verify the real launchd GUI domain and existing
job without changing either:

```sh
id -u
launchctl print "gui/$(id -u)"
launchctl print "gui/$(id -u)/com.swarmedge.parallax-sports"
```

Inspect the active production plist, its referenced release SHA, deployment
manifest, and runtime configuration by key names only. Compare manifest SHA to
the resolved release directory and record any drift. Check that BALLDONTLIE
credentials are owner-readable only; do not print them. Verify current source
terms and access tier. For NHL automated schedule access, set
`PARALLAX_NHL_EVIDENCE_AUTHORIZATION_REF` to a local reference to written
authorization. A URL alone is not authorization. The runner refuses to start
without this reference. Do not activate when the GUI domain is unresolved,
manifest drift is unexplained, or either league source lacks permission.

## Stage and activate (only after every preflight check passes)

Use the repository's release staging utility to create an immutable release at
the exact reviewed commit SHA; inspect that release's `release.json` and verify
its source SHA before continuing. Substitute absolute paths, and render the
plist template without changing its label or interval:

```sh
python3 scripts/migration_preflight.py create-release --source "$SOURCE" --sha "$SHA" --target-release "$RELEASE"
install -d -m 700 "$HOME/Library/LaunchAgents" "$HOME/.local/share/parallax/league-evidence" "$HOME/.local/state/parallax/league-evidence"
# Render @PYTHON@, @RELEASE_ROOT@, @EVIDENCE_DIR@, @STATE_DIR@, and
# @NHL_AUTHORIZATION_REFERENCE@ (the locally verified written authorization
# reference) into a temp file.
plutil -lint "$HOME/Library/LaunchAgents/com.swarmedge.parallax-league-evidence.plist"
install -m 600 "$RENDERED_PLIST" "$HOME/Library/LaunchAgents/com.swarmedge.parallax-league-evidence.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.swarmedge.parallax-league-evidence.plist"
launchctl kickstart "gui/$(id -u)/com.swarmedge.parallax-league-evidence"
```

Verify `launchctl print gui/$(id -u)/com.swarmedge.parallax-league-evidence`,
then require two consecutive hourly runs with each league's `last_success`
`updated_at` fresh, `snapshots_written > 0`, zero stale snapshots, and valid
JSONL rows whose `rules_sha256`, fixture identity, source time, and executable
depth are populated. Check all evidence, health, state, and log files are owned
by the user and mode 0600 (directories 0700). Keep logs bounded by the runner's
rotation policy; launchd stdout/stderr files should be cleared/rotated only
within the evidence state directory.

## Rollback

Rollback only this isolated job: `launchctl bootout
gui/$(id -u)/com.swarmedge.parallax-league-evidence`, then remove only its
dedicated plist and evidence/state directories after preserving any evidence
needed for analysis. Never bootout, edit, or reload the production sports job.
