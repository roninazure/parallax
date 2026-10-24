"""NBA-only launchd templates: static validation, no service activation."""
from pathlib import Path
import plistlib


ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "ops" / "launchd"
USER = "/Users/scottsteele/parallax-research"
WORKTREE = USER + "/parallax-shadow-nba"
VENV_PYTHON = USER + "/venv312/bin/python"


def read(name: str) -> dict:
    with (OPS / name).open("rb") as stream:
        return plistlib.load(stream)


def test_nba_scan_template_is_bounded_research_only_and_independent():
    config = read("com.swarmaxis.parallax-nba-shadow-research.plist")
    assert config["Label"] == "com.swarmaxis.parallax-nba-shadow-research"
    assert config["UserName"] == "scottsteele"
    assert config["ProgramArguments"] == [
        VENV_PYTHON, "-m", "scripts.nba_shadow_slate_research",
        "--max-games", "2", "--write-research-snapshot", "--summary-only",
    ]
    assert config["WorkingDirectory"] == WORKTREE
    assert config["EnvironmentVariables"]["PYTHONPATH"] == WORKTREE
    assert config["StartInterval"] == 7200
    assert config["RunAtLoad"] is True
    assert config["KeepAlive"] is False
    assert config["StandardOutPath"] == USER + "/nba/shadow-research.log"
    assert config["StandardErrorPath"] == USER + "/nba/shadow-research.err"


def test_nba_final_reconcile_template_is_independent_daily_and_read_only():
    config = read("com.swarmaxis.parallax-nba-forward-reconcile.plist")
    assert config["Label"] == "com.swarmaxis.parallax-nba-forward-reconcile"
    assert config["UserName"] == "scottsteele"
    assert config["ProgramArguments"] == [
        VENV_PYTHON, "-m", "scripts.nba_forward_reconcile", "--live",
    ]
    assert config["WorkingDirectory"] == WORKTREE
    assert config["EnvironmentVariables"]["PYTHONPATH"] == WORKTREE
    assert config["StartInterval"] == 86400
    assert config["RunAtLoad"] is True
    assert config["KeepAlive"] is False
    assert config["StandardOutPath"] == USER + "/nba/forward-reconcile.log"
    assert config["StandardErrorPath"] == USER + "/nba/forward-reconcile.err"


def test_nba_plists_cannot_modify_other_parallax_lanes_or_use_root_execution():
    labels = set()
    for name in (
        "com.swarmaxis.parallax-nba-shadow-research.plist",
        "com.swarmaxis.parallax-nba-forward-reconcile.plist",
    ):
        config = read(name)
        assert config["Label"] not in labels
        labels.add(config["Label"])
        assert config["UserName"] == "scottsteele"
        assert config["EnvironmentVariables"]["PYTHONDONTWRITEBYTECODE"] == "1"
        assert config["WorkingDirectory"] == WORKTREE
        assert not any(
            token in " ".join(config["ProgramArguments"]).lower()
            for token in ("ntfy", "--trade", "--buy", "--publish", "--execute")
        )
    assert len(labels) == 2
