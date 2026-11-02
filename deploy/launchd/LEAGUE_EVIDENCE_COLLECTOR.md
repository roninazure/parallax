# Isolated NBA/NHL evidence collector

The user LaunchAgent template is **NBA-only** while NHL automated-source
permission remains unverified. It never loads or reloads
`com.swarmedge.parallax-sports`. Run it from a SHA-pinned immutable release
with dedicated evidence and state directories, never production state or feeds.

## Read-only Mac mini preflight

Run this one command in the Mac mini user session. It checks the GUI domain,
loaded production sports job, active release SHA vs manifest, NBA credential
file ownership/mode, and secret-free authorization status. It prints no
credential values or authorization contents and changes no state:

```sh
python3 scripts/league_evidence_preflight.py --runtime-root "$HOME/Library/Application Support/SwarmEdge" --authorization-file "$HOME/.config/parallax/league-evidence-authorization.json"
```

The NBA authorization JSON must be prepared only after independently checking
the account's current BALLDONTLIE plan and reviewing current terms. Required
NBA fields: `provider: "BALLDONTLIE"`, nonempty `api_plan`,
`nba_games_access: true`, `account_plan_verified: true`,
`account_plan_verified_at` (ISO date within 90 days), `terms_reviewed: true`,
`terms_reviewed_at` (ISO date within one year), and `terms_url` exactly
`https://www.balldontlie.io/terms.html`. API key possession alone is not proof
of plan rights. The actual key stays in the existing owner-only credential file.

NHL remains disabled unless separately verified written permission is recorded
with `provider: "NHL"`, `automated_schedule_access: true`, a nonempty
`written_authorization_ref` identifying the actual permission, and a recent
`verified_at` ISO date. A URL alone does not establish permission. Authorization
is recorded per league and never inferred. `--leagues NBA` makes no NHL fixture
or market requests; the LaunchAgent template explicitly selects NBA only.
Do not activate if the production job cannot be resolved as **running**, its
loaded plist cannot be inspected, its release identity differs from the
manifest, the NBA credential file is unsafe, or NBA account/plan/terms
authorization is unverified. The preflight resolves the system LaunchDaemon
before checking user domains; a GUI session is not required when the system
job is available.

The preflight is stricter than a Git SHA comparison: it reads the **loaded**
launchd plist identified by `launchctl print` (not a possibly stale staging
candidate), verifies the release root and `PARALLAX_RELEASE_SHA`, and checks
SHA-256 for the loaded plist, runtime environment file, scheduler entrypoint,
and dependency lock against the deployment manifest. A missing/unreadable job
path, incomplete manifest, or any drift returns
`sports_manifest_content_hashes_unverified` and **blocks** collection. No
secrets or raw runtime environment values are printed. Never resolve a
mismatch by editing the manifest in place: stage a new immutable SHA release
and regenerate its manifest from the actual release inputs through the
separately authorized deployment process in `deploy/README.md`.

## Stage, install, and verify after preflight passes

Stage an immutable release at the exact reviewed SHA and inspect its
`release.json` before installing the isolated plist:

```sh
python3 scripts/migration_preflight.py create-release --source "$SOURCE" --sha "$SHA" --target-release "$RELEASE"
install -d -m 700 "$HOME/Library/LaunchAgents" "$HOME/.local/share/parallax/league-evidence" "$HOME/.local/state/parallax/league-evidence"
# Render @PYTHON@, @RELEASE_ROOT@, @EVIDENCE_DIR@, @STATE_DIR@, and
# @AUTHORIZATION_FILE@ into a temporary plist.
plutil -lint "$HOME/Library/LaunchAgents/com.swarmedge.parallax-league-evidence.plist"
install -m 600 "$RENDERED_PLIST" "$HOME/Library/LaunchAgents/com.swarmedge.parallax-league-evidence.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.swarmedge.parallax-league-evidence.plist"
launchctl kickstart "gui/$(id -u)/com.swarmedge.parallax-league-evidence"
```

Verify the isolated label with `launchctl print`, then require two consecutive
hourly NBA runs with fresh `last_success`, `snapshots_written > 0`, zero stale
snapshots, and valid JSONL rows containing fixture identity, rules hash, source
time, and executable depth. All files must be user-owned and mode 0600, with
directories mode 0700. Logs rotate at 2 MB with five backups.

Empty schedule and no discovered markets are reported as `EMPTY` and do not
change `last_success`. Failed provider acquisition, identity mapping, book
acquisition, or invalid executable quotes are `FAILED` and return nonzero.

## Rollback

Rollback only the isolated job with
`launchctl bootout gui/$(id -u)/com.swarmedge.parallax-league-evidence`, then
remove only its dedicated plist and evidence/state directories if appropriate.
Never bootout, edit, or reload the production sports job.
