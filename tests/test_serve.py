"""The supervised topology: python -m rssgate is the live entry point (no
"run.py" in argv, so another project's `pkill -f run.py` - which killed the
server on 2026-10-21 - cannot reach us), run.py stays a working shim, and
the supervision scripts are syntactically sound and launch the right thing."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(*cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          timeout=60)


def test_module_entry_point():
    r = _run(sys.executable, "-m", "rssgate", "--help")
    assert r.returncode == 0
    assert "--config" in r.stdout


def test_run_py_shim_still_works():
    r = _run(sys.executable, "run.py", "--help")
    assert r.returncode == 0
    assert "--config" in r.stdout


def test_serve_scripts_are_valid_and_launch_the_module():
    for s in ("scripts/serve-loop.sh", "scripts/ensure-serving.sh"):
        assert _run("bash", "-n", os.path.join(ROOT, s)).returncode == 0, s
    loop = open(os.path.join(ROOT, "scripts/serve-loop.sh")).read()
    assert ".venv/bin/python -m rssgate" in loop
    assert "python run.py" not in loop, "the loop must not resurrect the old argv"
    ensure = open(os.path.join(ROOT, "scripts/ensure-serving.sh")).read()
    # the pgrep pattern must be the absolute path: filmNotes' loop matches
    # the generic "scripts/serve-loop.sh" and would masquerade as ours
    assert "pgrep -f \"$APP/scripts/serve-loop.sh\"" in ensure
