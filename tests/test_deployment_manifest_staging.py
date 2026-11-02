"""Synthetic, host-independent rehearsal of immutable Mac mini manifest staging."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deployment_manifest.py"


def test_immutable_release_manifest_staged_externally_and_detects_plist_drift(tmp_path):
    sha = "a" * 40
    release = tmp_path / "releases" / sha
    (release / "scripts").mkdir(parents=True)
    lock = release / "requirements.lock"
    lock.write_text("example==1.0\n", encoding="utf-8")
    entrypoint = release / "scripts/parallax_unattended.py"
    entrypoint.write_text("print('read-only test')\n", encoding="utf-8")
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text("TEST_ONLY=1\n", encoding="utf-8")
    plist = tmp_path / "candidate.plist"
    plist.write_text("<plist>candidate</plist>\n", encoding="utf-8")

    # Mirror create-release's read-only permissions; no generated files may
    # appear under the immutable release.
    lock.chmod(0o444)
    entrypoint.chmod(0o444)
    (release / "scripts").chmod(0o555)
    release.chmod(0o555)

    manifest = tmp_path / "run" / "candidates" / f"{sha}.manifest.json"
    common = [
        "--root", str(release),
        "--runtime-env", str(runtime_env),
        "--plist", str(plist),
        "--lock", str(lock),
        "--entrypoint", str(entrypoint),
    ]

    def run(command, extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), command, *common, *extra],
            capture_output=True, text=True, check=False,
        )

    generated = run("generate", ["--output", str(manifest)])
    assert generated.returncode == 0, generated.stderr
    assert manifest.is_file()
    assert json.loads(manifest.read_text(encoding="utf-8"))["git_sha"] == sha
    assert not (release / "deploy" / "manifest.json").exists()

    valid = run("validate", ["--manifest", str(manifest)])
    assert valid.returncode == 0, valid.stderr

    # Staged plist drift must fail validation without modifying production.
    plist.write_text("<plist>changed</plist>\n", encoding="utf-8")
    invalid = run("validate", ["--manifest", str(manifest)])
    assert invalid.returncode == 1
    assert "validation failed" in invalid.stdout
