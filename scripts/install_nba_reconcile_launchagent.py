#!/usr/bin/env python3
"""Install isolated NBA research-scorecard LaunchAgent (no production changes).

Only the current macOS user's GUI launchd domain is used. Requires login to run.
"""
from __future__ import annotations
import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.swarmedge.parallax-nba-reconcile"

def install(*, source: Path, snapshots: Path, state: Path, home: Path,
            python: Path, invoke=subprocess.run) -> dict:
    script = source / "scripts" / "nba_research_daily_reconcile.py"
    if not script.is_file() or not snapshots.is_dir():
        raise ValueError("reviewed NBA script or forecast directory missing")
    if not python.is_file():
        raise ValueError("Python interpreter missing")
    target = home / "Library" / "LaunchAgents" / (LABEL + ".plist")
    if target.exists():
        raise ValueError("NBA LaunchAgent already exists; not overwriting")
    if state.is_symlink() or source.is_symlink():
        raise ValueError("unsafe symlink path")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(python), str(script),
                             "--snapshots", str(snapshots),
                             "--state", str(state)],
        "StartCalendarInterval": {"Hour": 12, "Minute": 30},
        "RunAtLoad": False,
        "StandardOutPath": str(state / "launchd.stdout.log"),
        "StandardErrorPath": str(state / "launchd.stderr.log"),
        "WorkingDirectory": str(source),
    }
    with target.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        plistlib.dump(payload, stream)
    try:
        check = invoke(["/usr/bin/plutil", "-lint", str(target)], capture_output=True, text=True)
        if check.returncode != 0:
            raise RuntimeError("LaunchAgent plist failed validation")
        domain = f"gui/{os.getuid()}"
        boot = invoke(["/bin/launchctl", "bootstrap", domain, str(target)],
                      capture_output=True, text=True)
        if boot.returncode != 0:
            raise RuntimeError(f"Unable to bootstrap NBA user LaunchAgent: {boot.stderr.strip()[:200]}")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {"status": "INSTALLED", "label": LABEL, "domain": f"gui/{os.getuid()}",
            "schedule": "daily at 12:30 local macOS time",
            "plist": str(target), "research_only": True,
            "first_run": "next scheduled slot, or explicit launchctl kickstart"}

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", required=True)
    args = parser.parse_args()
    home = Path.home()
    src = Path(__file__).resolve().parents[1]
    snapshots = home / "parallax-research/nba/forward"
    state = home / "parallax-research/nba/phase1/unattended"
    result = install(source=src, snapshots=snapshots, state=state, home=home,
                     python=Path(sys.executable))
    import json
    print(json.dumps(result, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
