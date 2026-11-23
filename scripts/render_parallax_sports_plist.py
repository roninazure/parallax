#!/usr/bin/env python3
"""Render and statically validate the production PARALLAX sports launchd plist.

No launchctl calls, service mutations, credentials or remote execution.
"""
from __future__ import annotations

import argparse
import plistlib
import re
from pathlib import Path

PLACEHOLDER = re.compile(r"@[A-Z][A-Z0-9_]*@")


def render_sports_plist(
    template: Path, output: Path, *, release_root: Path,
    python_path: Path, state_dir: Path, runtime_env: Path,
    log_dir: Path, release_sha: str,
) -> None:
    paths = [release_root, python_path, state_dir, runtime_env, log_dir, output]
    if any(not path.is_absolute() for path in paths):
        raise ValueError("Every deployment path must be absolute")
    if not re.fullmatch(r"[a-fA-F0-9]{40}", release_sha):
        raise ValueError("Release SHA must be a full 40-character Git SHA")
    if release_root.name.lower() != release_sha.lower():
        raise ValueError("Release root must end in the exact release SHA")
    replacements = {
        "@SWARM_EDGE_ROOT@": str(release_root),
        "@SWARM_EDGE_PYTHON@": str(python_path),
        "@PARALLAX_STATE_DIR@": str(state_dir),
        "@BGL_RUNTIME_ENV_FILE@": str(runtime_env),
        "@SWARM_EDGE_LOG_DIR@": str(log_dir),
        "@RELEASE_SHA@": release_sha,
    }
    content = template.read_text(encoding="utf-8")
    for token, replacement in replacements.items():
        content = content.replace(token, replacement)
    leftover = PLACEHOLDER.findall(content)
    if leftover:
        raise ValueError(f"Unresolved plist placeholders: {', '.join(sorted(set(leftover)))}")
    parsed = plistlib.loads(content.encode("utf-8"))
    if parsed.get("Label") != "com.swarmedge.parallax-sports":
        raise ValueError("Not a PARALLAX sports launchd service")
    args = parsed.get("ProgramArguments", [])
    if not args or args[0] != str(python_path):
        raise ValueError("Rendered plist has incorrect venv Python")
    if str(release_root / "scripts" / "parallax_unattended.py") not in args:
        raise ValueError("Rendered plist has incorrect sports entrypoint")
    if parsed.get("EnvironmentVariables", {}).get("PARALLAX_RELEASE_SHA") != release_sha:
        raise ValueError("Rendered plist release SHA mismatch")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing plist: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    output.chmod(0o600)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--template", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--release-root", type=Path, required=True)
    p.add_argument("--python", type=Path, required=True)
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--runtime-env", type=Path, required=True)
    p.add_argument("--log-dir", type=Path, required=True)
    p.add_argument("--release-sha", required=True)
    args = p.parse_args()
    render_sports_plist(
        args.template, args.output, release_root=args.release_root,
        python_path=args.python, state_dir=args.state_dir,
        runtime_env=args.runtime_env, log_dir=args.log_dir,
        release_sha=args.release_sha,
    )


if __name__ == "__main__":
    main()
