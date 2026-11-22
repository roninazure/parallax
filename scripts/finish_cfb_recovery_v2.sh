#!/usr/bin/env bash
# CFB v2 production cutover: old verified NFL baseline + CI-tested CFB-only overlay.
# Stops on any failed preflight; never mutates the existing immutable release.
set -Eeuo pipefail
umask 077

OLD_SHA="eeb1c6215e880efc2cb0446b0e174d01886ec416"
LIVE_SHA="46e87dfa7a7697488d14f19d83f4479031cde510"
PATCH_SHA="063034d6601a1b56baf235a2c46dc3ad7823a004"
PATCH_BRANCH="fix/cfb-reserve-kalshi-book-budget-20261122"
SRC="/Users/scottsteele/swarm-runtime/swarm-edge"
BASE="$HOME/Library/Application Support/SwarmEdge"
OLD_PY="$BASE/venvs/$OLD_SHA/bin/python"
SERVICE="system/com.swarmedge.parallax-sports"

log() { printf '[CFB RECOVERY] %s\n' "$*"; }
fail() { printf '[CFB RECOVERY] BLOCKED: %s\n' "$*" >&2; exit 1; }
trap 'printf "[CFB RECOVERY] FAILED at line %s. Existing immutable release and backup retained.\n" "$LINENO" >&2' ERR

[[ "$(id -un)" == "scottsteele" ]] || fail "Run from the scottsteele Mac mini account."
[[ -x "$OLD_PY" ]] || fail "Known-good production Python missing."
[[ -d "$SRC" ]] || fail "Mac mini source repository missing."
[[ -f "$SRC/scripts/deploy_parallax_sports.py" ]] || fail "Verified deploy utility unavailable."
[[ -f "$BASE/config/runtime.env" ]] || fail "Runtime configuration missing."
[[ -f "$SRC/scripts/migration_preflight.py" ]] || fail "Immutable release packager unavailable."

log "Checking active NFL release before modifying any staging files."
sudo -v
sudo launchctl print "$SERVICE" | grep -F "$LIVE_SHA" >/dev/null ||
    fail "Production no longer matches the verified 46e87dfa release; aborting."
git -C "$SRC" cat-file -e "$OLD_SHA^{commit}" ||
    fail "Local immutable base commit is not available; refusing to substitute GitHub main."

log "Fetching pinned CFB candidate $PATCH_SHA (no checkout of live source)."
git -C "$SRC" fetch --no-tags https://github.com/roninazure/parallax.git "$PATCH_BRANCH"
git -C "$SRC" cat-file -e "$PATCH_SHA^{commit}" ||
    fail "Pinned CFB candidate was not fetched from GitHub."

mkdir -p "$BASE/staging"
WORKROOT="$(mktemp -d "$BASE/staging/cfb-recovery.XXXXXX")"
WT="$WORKROOT/worktree"
git -C "$SRC" worktree add --detach "$WT" "$OLD_SHA"
FILES=(
  scripts/cfb_live_scan.py
  parallax/data/cfb_pmus_team_codes.json
  tests/test_cfb_kalshi_discovery_budget.py
  tests/test_parallax_live_cfb.py
)
for file in "${FILES[@]}"; do
  mkdir -p "$WT/$(dirname "$file")"
  git -C "$SRC" show "$PATCH_SHA:$file" > "$WT/$file"
done

TEST_PY="$SRC/.venv/bin/python"
[[ -x "$TEST_PY" ]] || TEST_PY="$OLD_PY"
log "Running CFB and NFL regression suites on the isolated NFL-baseline overlay."
(
  cd "$WT"
  PYTHONPATH="$WT" "$TEST_PY" -m pytest -q \
    tests/test_cfb_kalshi_discovery_budget.py \
    tests/test_parallax_live_cfb.py \
    tests/test_parallax_cfb.py \
    tests/test_parallax_nfl.py
) || fail "Candidate regression suite failed; no deployment performed."

git -C "$WT" add -- "${FILES[@]}"
git -C "$WT" -c user.name="SwarmEdge Local Recovery" \
  -c user.email="recovery@localhost" commit -q \
  -m "fix(cfb): integrate tested PMUS and Kalshi restoration over NFL eeb1 baseline"
NEW_SHA="$(git -C "$WT" rev-parse HEAD)"
CHANGED="$(git -C "$WT" diff-tree --no-commit-id --name-only -r "$NEW_SHA")"
while IFS= read -r file; do
  [[ -z "$file" ]] && continue
  case "$file" in
    scripts/cfb_live_scan.py|parallax/data/cfb_pmus_team_codes.json|tests/test_cfb_kalshi_discovery_budget.py|tests/test_parallax_live_cfb.py) ;;
    *) fail "Unexpected production change: $file" ;;
  esac
done <<< "$CHANGED"

RELEASE="$BASE/releases/$NEW_SHA"
VENV="$BASE/venvs/$NEW_SHA"
STAGE="$BASE/staging/$NEW_SHA"
PLIST="$STAGE/com.swarmedge.parallax-sports.plist"
MANIFEST="$STAGE/deployment-manifest.json"
[[ ! -e "$RELEASE" && ! -e "$VENV" && ! -e "$STAGE" ]] ||
    fail "Candidate already staged; refusing to overwrite immutable artifacts."

log "Building immutable candidate release $NEW_SHA."
"$TEST_PY" "$WT/scripts/migration_preflight.py" create-release \
  --source "$WT" --sha "$NEW_SHA" --target-release "$RELEASE"

log "Building release-isolated Python environment."
"$OLD_PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --disable-pip-version-check \
  -r "$RELEASE/requirements.lock"
"$VENV/bin/python" -m pip check

mkdir -p "$STAGE"
"$VENV/bin/python" - \
  "$RELEASE/deploy/launchd/com.swarmedge.parallax-sports.plist.in" \
  "$PLIST" "$BASE" "$RELEASE" "$VENV/bin/python" "$NEW_SHA" <<'PY'
from pathlib import Path
import re
import sys
source, output, base, release, python, sha = sys.argv[1:]
base = Path(base)
template = Path(source).read_text(encoding="utf-8")
replacements = {
    "@SWARM_EDGE_ROOT@": release,
    "@SWARM_EDGE_PYTHON@": python,
    "@PARALLAX_STATE_DIR@": str(base / "state/parallax"),
    "@BGL_RUNTIME_ENV_FILE@": str(base / "config/runtime.env"),
    "@SWARM_EDGE_LOG_DIR@": str(Path.home() / "Library/Logs/SwarmEdge"),
    "@RELEASE_SHA@": sha,
}
for key, value in replacements.items():
    template = template.replace(key, value)
if re.search(r"@[A-Z_]+@", template):
    raise SystemExit("Unresolved plist template variable")
path = Path(output)
path.write_text(template, encoding="utf-8")
path.chmod(0o600)
PY
plutil -lint "$PLIST"

"$VENV/bin/python" "$WT/scripts/deployment_manifest.py" generate \
  --root "$RELEASE" --runtime-env "$BASE/config/runtime.env" \
  --plist "$PLIST" --lock "$RELEASE/requirements.lock" \
  --entrypoint "$RELEASE/scripts/parallax_unattended.py" \
  --output "$MANIFEST"
"$VENV/bin/python" "$WT/scripts/deployment_manifest.py" validate \
  --root "$RELEASE" --runtime-env "$BASE/config/runtime.env" \
  --plist "$PLIST" --lock "$RELEASE/requirements.lock" \
  --entrypoint "$RELEASE/scripts/parallax_unattended.py" \
  --manifest "$MANIFEST"

sudo launchctl print "$SERVICE" | grep -F "$LIVE_SHA" >/dev/null ||
    fail "Active service changed during staging; expected 46e87dfa release; refusing cutover."
log "Running previously accepted guarded deployment utility with automatic rollback."
sudo "$TEST_PY" "$SRC/scripts/deploy_parallax_sports.py" \
  --apply --authorize-cutover "$NEW_SHA" --sha "$NEW_SHA" \
  --release "$RELEASE" --venv "$VENV" --plist "$PLIST" \
  --manifest "$MANIFEST" --state-dir "$BASE/state/parallax" \
  --backups "$BASE/backups"

sudo launchctl print "$SERVICE" | grep -F "$VENV/bin/python" >/dev/null ||
    fail "Deployment utility did not leave the new release active."
log "Cutover accepted with isolated NFL baseline and rollback history retained."
log "New CFB release: $NEW_SHA"
"$VENV/bin/python" - "$BASE/state/parallax/parallax_unattended_health.json" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.exists():
    print("HEALTH: pending; no health record found")
else:
    d = json.loads(p.read_text(encoding="utf-8"))
    print("HEALTH:", d.get("state"))
    print("CFB LAST SUCCESS:", d.get("last_successful_cfb_scan"))
    print("RECENT CFB ERRORS:", [
        str(e)[:200] for e in d.get("recent_errors", [])
        if "cfb" in str(e).lower()
    ])
PY
