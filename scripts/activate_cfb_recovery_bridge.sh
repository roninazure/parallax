#!/usr/bin/env bash
# Temporary CFB-only recovery bridge. Never bootout/kickstart the NFL sports service.
set -Eeuo pipefail
umask 077
[[ "$(id -un)" == "scottsteele" ]] || { echo "BLOCKED: run as scottsteele on Mac mini" >&2; exit 1; }

BASE="$HOME/Library/Application Support/SwarmEdge"
REPO="$HOME/swarm-runtime/swarm-edge"
NFL_SHA="46e87dfa7a7697488d14f19d83f4479031cde510"
CFB_SHA="a52d71f2f30c708782bd67bf8f7e3f8fee870923"
WORKER_SHA="ba2216bac74d84c578ef3250a5d483b0356ff841"
NFL_JOB="system/com.swarmedge.parallax-sports"
CFB_LABEL="com.swarmedge.parallax-cfb-restore"
CFB_JOB="system/$CFB_LABEL"
OPS="$BASE/ops/cfb-restore"
WORKER="$OPS/cfb_recovery_bridge.py"
RELEASE="$BASE/releases/$CFB_SHA"
PY="$BASE/venvs/$CFB_SHA/bin/python"
ENV_FILE="$BASE/config/runtime.env"
STATE="$BASE/state/parallax"
HEALTH="$STATE/cfb_recovery_bridge_health.json"
PLIST="/Library/LaunchDaemons/$CFB_LABEL.plist"
LOGS="$HOME/Library/Logs/SwarmEdge"
BACKUP="$OPS/runtime.env.before-cfb-bridge"
fail() { printf 'CFB BRIDGE BLOCKED: %s\n' "$*" >&2; exit 1; }

[[ -x "$PY" && -f "$RELEASE/scripts/cfb_live_scan.py" ]] || fail "staged CFB v4 release missing"
[[ -d "$REPO/.git" || -f "$REPO/.git" ]] || fail "Mac mini git repository missing"
[[ -f "$ENV_FILE" ]] || fail "runtime.env unavailable"
[[ -w "$ENV_FILE" ]] || fail "runtime.env is not writable by scottsteele"
[[ ! -e "$BACKUP" && ! -e "$HEALTH" ]] || fail "bridge was already attempted; do not overwrite backups or health"
[[ ! -e "$PLIST" ]] || fail "CFB launchd job already installed"
sudo launchctl print "$CFB_JOB" >/dev/null 2>&1 && fail "CFB job already loaded"
NFL_BEFORE="$(sudo launchctl print "$NFL_JOB")"
grep -Fq "PARALLAX_RELEASE_SHA => $NFL_SHA" <<< "$NFL_BEFORE" || fail "NFL release no longer matches approved baseline"
grep -Fq 'state = running' <<< "$NFL_BEFORE" || fail "NFL service not running"
grep -Eq 'pid = [0-9]+' <<< "$NFL_BEFORE" || fail "NFL PID missing"
python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import re, sys
lines = Path(sys.argv[1]).read_text().splitlines()
matches = [v.group(1) for line in lines if (v:=re.fullmatch(r'\s*PARALLAX_CFB_ENABLED\s*=\s*["\x27]?(.*?)["\x27]?\s*', line))]
if len(matches) != 1 or matches[0].casefold() not in {"true", "1", "on", "yes"}:
    raise SystemExit("BLOCKED: expected exactly one enabled PARALLAX_CFB_ENABLED setting")
PY

mkdir -p "$OPS" "$LOGS" "$STATE"
chmod 700 "$OPS"
cp -p "$ENV_FILE" "$BACKUP"
BRIDGE_LOADED=0
BRIDGE_ACTIVATED=0
cleanup_failure() {
  code=$?
  trap - EXIT
  if [[ "$BRIDGE_ACTIVATED" != 1 && "$BRIDGE_LOADED" == 1 ]]; then
    sudo launchctl bootout "$CFB_JOB" >/dev/null 2>&1 || true
    sudo rm -f "$PLIST"
    echo "CFB bridge was not activated; auxiliary job removed, original CFB setting retained." >&2
  fi
  exit "$code"
}
trap cleanup_failure EXIT
git -C "$REPO" cat-file blob "$WORKER_SHA:scripts/cfb_recovery_bridge.py" > "$WORKER"
head -1 "$WORKER" | grep -Fxq '#!/usr/bin/env python3' || fail "invalid recovery worker"
"$PY" -m py_compile "$WORKER"
chmod 700 "$WORKER"
"$PY" "$WORKER" --preflight --release "$RELEASE" --runtime-env "$ENV_FILE" --state-dir "$STATE" || fail "CFB bridge alert configuration preflight failed"

# An independent system job means a bootstrap failure cannot stop NFL.
STAGED_PLIST="$OPS/$CFB_LABEL.plist"
"$PY" - "$STAGED_PLIST" "$CFB_LABEL" "$PY" "$WORKER" "$RELEASE" "$ENV_FILE" "$STATE" "$LOGS" <<'PY'
import plistlib, sys
out, label, python, worker, release, config, state, logs = sys.argv[1:]
data = {
    "Label": label,
    "ProgramArguments": [python, worker, "--release", release, "--runtime-env", config, "--state-dir", state],
    "WorkingDirectory": state,
    "UserName": "scottsteele",
    "EnvironmentVariables": {"HOME": "/Users/scottsteele", "PYTHONPATH": release},
    "RunAtLoad": True,
    "StartInterval": 3600,
    "KeepAlive": False,
    "StandardOutPath": logs + "/parallax-cfb-restore.log",
    "StandardErrorPath": logs + "/parallax-cfb-restore.err.log",
}
with open(out, "wb") as stream:
    plistlib.dump(data, stream)
PY
plutil -lint "$STAGED_PLIST" || fail "CFB plist validation failed"
sudo install -o root -g wheel -m 644 "$STAGED_PLIST" "$PLIST"
echo "Starting independent CFB lane (NFL remains running)."
if ! sudo launchctl bootstrap system "$PLIST"; then
  echo "CFB bootstrap failed; original CFB remains enabled. NFL untouched." >&2
  exit 1
fi
BRIDGE_LOADED=1
sudo launchctl print "$CFB_JOB" >/dev/null || fail "CFB job not loaded"

# Require a real completed scan and feed export before switching CFB ownership.
for attempt in $(seq 1 150); do
  [[ -s "$HEALTH" ]] && break
  if ! sudo launchctl print "$CFB_JOB" >/dev/null; then fail "CFB job disappeared"; fi
  sleep 2
done
[[ -s "$HEALTH" ]] || fail "CFB scan did not pass. Original CFB remains enabled; inspect restore logs."
"$PY" - "$HEALTH" "$CFB_SHA" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
if data.get("status") != "PASS" or data.get("release") != sys.argv[2] or data.get("scored_positions", 0) < 1:
    raise SystemExit("BLOCKED: CFB bridge health not verified")
print("CFB first live scan:", json.dumps(data, sort_keys=True))
PY

# Only disable the old CFB lane after its independent replacement passed.
"$PY" - "$ENV_FILE" <<'PY'
import os, re, stat, sys, tempfile
from pathlib import Path
path = Path(sys.argv[1])
original = path.read_text()
lines = original.splitlines(keepends=True)
found = []
for i, line in enumerate(lines):
    match = re.fullmatch(r'(\s*PARALLAX_CFB_ENABLED\s*=\s*)["\x27]?(true|1|on|yes)["\x27]?(\s*(?:\n|$))', line, flags=re.I)
    if match:
        found.append(i)
        lines[i] = match.group(1) + '0' + match.group(3)
if len(found) != 1:
    raise SystemExit("BLOCKED: original CFB configuration no longer matches expected state")
mode = stat.S_IMODE(path.stat().st_mode)
with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".cfb-bridge.", delete=False) as f:
    f.write("".join(lines))
    f.flush()
    os.fsync(f.fileno())
    tmp = f.name
os.chmod(tmp, mode)
os.replace(tmp, path)
PY
NFL_AFTER="$(sudo launchctl print "$NFL_JOB")"
if ! grep -Fq "PARALLAX_RELEASE_SHA => $NFL_SHA" <<< "$NFL_AFTER" || ! grep -Fq 'state = running' <<< "$NFL_AFTER"; then
  cp -p "$BACKUP" "$ENV_FILE"
  fail "NFL service unexpected change: original runtime env restored"
fi
BRIDGE_ACTIVATED=1
trap - EXIT
echo "CFB BRIDGE ACTIVE: new CFB release scans hourly and publishes shared feed."
echo "NFL service remains running on $NFL_SHA; original CFB lane disabled via runtime.env only."
echo "Rollback config backup: $BACKUP"
echo "Auxiliary launchd job: $CFB_JOB"
