"""Offline install/rollback checks for isolated NBA research LaunchAgent."""
import importlib.util
import plistlib
from pathlib import Path
from types import SimpleNamespace
import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts/install_nba_reconcile_launchagent.py"
spec = importlib.util.spec_from_file_location("install_nba_reconcile_launchagent", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def env(tmp_path):
    src, snapshots, state, home = (tmp_path/"code", tmp_path/"forecasts",
                                   tmp_path/"state", tmp_path/"home")
    (src/"scripts").mkdir(parents=True)
    (src/"scripts"/"nba_research_daily_reconcile.py").write_text("print('safe')")
    snapshots.mkdir()
    home.mkdir()
    python = tmp_path/"python3"
    python.write_text("binary-placeholder")
    return dict(source=src, snapshots=snapshots, state=state, home=home, python=python)

def test_installs_only_user_agent_and_correct_paths(tmp_path):
    args = env(tmp_path)
    invoked = []
    def invoke(command, **kwargs):
        invoked.append(command)
        return SimpleNamespace(returncode=0, stderr="")
    report = module.install(**args, invoke=invoke)
    assert report["status"] == "INSTALLED"
    assert len(invoked) == 2
    assert invoked[0][0] == "/usr/bin/plutil"
    assert invoked[1][:3] == ["/bin/launchctl", "bootstrap", f"gui/{module.os.getuid()}"]
    plist = plistlib.loads(Path(report["plist"]).read_bytes())
    assert plist["StartCalendarInterval"] == {"Hour":12, "Minute":30}
    assert plist["RunAtLoad"] is False
    assert plist["ProgramArguments"][2] == "--snapshots"
    assert "parallax-sports" not in str(invoked)
    assert Path(report["plist"]).stat().st_mode & 0o777 == 0o600

def test_refuses_existing_without_reloading(tmp_path):
    args = env(tmp_path)
    inv = lambda *a, **kw: SimpleNamespace(returncode=0, stderr="")
    module.install(**args, invoke=inv)
    with pytest.raises(ValueError, match="already exists"):
        module.install(**args, invoke=inv)

def test_failed_bootstrap_removes_candidate_plist(tmp_path):
    args = env(tmp_path)
    def invoke(command, **kwargs):
        return SimpleNamespace(returncode=1 if command[1] == "bootstrap" else 0,
                               stderr="no session")
    with pytest.raises(RuntimeError, match="bootstrap"):
        module.install(**args, invoke=invoke)
    assert not list((args["home"]/"Library"/"LaunchAgents").glob("*.plist"))
