from pathlib import Path

import pytest

from scripts.render_parallax_sports_plist import render_sports_plist

TEMPLATE = Path(__file__).resolve().parents[1] / "deploy/launchd/com.swarmedge.parallax-sports.plist.in"
SHA = "a" * 40


def _args(tmp_path):
    return {
        "release_root": tmp_path / "releases" / SHA,
        "python_path": tmp_path / "venvs" / SHA / "bin/python3",
        "state_dir": tmp_path / "state",
        "runtime_env": tmp_path / "config/runtime.env",
        "log_dir": tmp_path / "logs",
        "release_sha": SHA,
    }


def test_sports_plist_renders_all_placeholders(tmp_path):
    output = tmp_path / "rendered/sports.plist"
    render_sports_plist(TEMPLATE, output, **_args(tmp_path))
    content = output.read_text()
    assert "@SWARM_EDGE_" not in content
    assert "@PARALLAX_" not in content
    assert "@BGL_" not in content
    assert "@RELEASE_SHA@" not in content
    assert "com.swarmedge.parallax-sports" in content
    assert "com.swarmedge.runner" not in content
    assert str(tmp_path / "venvs" / SHA / "bin/python3") in content
    assert output.stat().st_mode & 0o777 == 0o600


def test_sports_plist_rejects_wrong_sha(tmp_path):
    with pytest.raises(ValueError, match="exact release SHA"):
        render_sports_plist(TEMPLATE, tmp_path / "rendered.plist", **{
            **_args(tmp_path), "release_sha": "b" * 40,
        })


def test_sports_plist_rejects_overwrite(tmp_path):
    output = tmp_path / "rendered.plist"
    render_sports_plist(TEMPLATE, output, **_args(tmp_path))
    with pytest.raises(FileExistsError):
        render_sports_plist(TEMPLATE, output, **_args(tmp_path))


def test_sports_plist_rejects_unknown_token(tmp_path):
    new_template = tmp_path / "bad.plist.in"
    new_template.write_text(TEMPLATE.read_text().replace(
        "@RELEASE_SHA@", "@RELEASE_SHA@@UNKNOWN_TOKEN@"
    ))
    with pytest.raises(ValueError, match="Unresolved"):
        render_sports_plist(new_template, tmp_path / "rendered.plist", **_args(tmp_path))
