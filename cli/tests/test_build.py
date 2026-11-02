"""tule build: reruns only what changed, builds what a script reads first, refuses other writes."""

import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("tule")

PLATE = """from tule import *
from lib.params import WALL
outputs.part("plate", rect(80, 40, center=True).extrude(WALL))
"""
BOM = """import csv
from tule import import_step, outputs
p = import_step("outputs/plate/plate.step")
prices = dict(csv.reader(open("inputs/prices.csv")))
with open(outputs.folder() + "/bom.csv", "w") as f:
    f.write(f"part,volume,price\\nplate,{p.volume:.0f},{prices['plate']}\\n")
"""


def build(root, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("TULE_WORKSPACE", "TULE_OUTPUTS")}
    r = subprocess.run([sys.executable, "-m", "tule_cli", "build", *args], cwd=root, env=env,
                       capture_output=True, text=True, timeout=300)
    return r.returncode, r.stderr


@pytest.fixture
def project(tmp_path):
    for d in ("src", "lib", "inputs"):
        (tmp_path / d).mkdir()
    (tmp_path / "tule.toml").write_text("")
    (tmp_path / "lib" / "params.py").write_text("WALL = 6\n")
    (tmp_path / "src" / "plate.py").write_text(PLATE)
    (tmp_path / "src" / "bom.py").write_text(BOM)
    (tmp_path / "inputs" / "prices.csv").write_text("plate,12.50\n")
    return tmp_path


def test_builds_dependencies_first_then_only_what_changed(project):
    code, err = build(project)
    assert code == 0, err
    assert err.index("plate: built") < err.index("bom: built")
    assert (project / "outputs" / "bom" / "bom.csv").read_text().endswith("plate,19200,12.50\n")
    rec = json.loads((project / "outputs" / ".build" / "bom.json").read_text())
    assert set(rec["reads"]) == {"src/bom.py", "inputs/prices.csv", "outputs/plate/plate.step"}

    assert build(project)[1].strip().endswith("2 up to date")

    (project / "inputs" / "prices.csv").write_text("plate,13.00\n")
    err = build(project)[1]
    assert "bom: building (inputs/prices.csv changed)" in err and "plate: building" not in err

    (project / "lib" / "params.py").write_text("WALL = 8\n")
    err = build(project)[1]
    assert "plate: building (lib/params.py changed)" in err and "bom: building (outputs/plate/plate.step changed)" in err
    assert (project / "outputs" / "bom" / "bom.csv").read_text().endswith("plate,25600,13.00\n")


def test_a_change_that_does_not_change_the_output_stops_there(project):
    build(project)
    with open(project / "src" / "plate.py", "a") as f:
        f.write("# a comment\n")
    err = build(project)[1]
    assert "plate: built" in err and "bom: building" not in err  # same STEP file: bom stays


def test_refuses_writes_outside_its_output_and_keeps_the_last_one(project):
    build(project)
    (project / "src" / "plate.py").write_text(PLATE + 'open("inputs/prices.csv", "w").write("x")\n')
    code, err = build(project, "plate")
    assert code == 1 and "writes inputs/prices.csv" in err
    assert (project / "inputs" / "prices.csv").read_text() == "plate,12.50\n"
    assert (project / "outputs" / "plate" / "plate.step").exists()


def test_a_script_writes_only_the_output_named_like_it(project):
    (project / "src" / "wrong.py").write_text('from tule import *\noutputs.part("other", box(1, 1, 1))\n')
    code, err = build(project, "wrong")
    assert code == 1 and "writes only outputs/wrong/" in err
    assert not (project / "outputs" / "other").exists()


def test_a_failed_dependency_fails_what_reads_it(project):
    (project / "src" / "plate.py").write_text("raise ValueError('boom')\n")
    code, err = build(project)
    assert code == 1
    assert "needs outputs/plate, which failed" in err


def test_a_failure_keeps_the_end_of_its_output(project):
    (project / "src" / "plate.py").write_text("print('step 1')\nraise ValueError('boom')\n")
    build(project, "plate")
    rec = json.loads((project / "outputs" / ".build" / "plate.json").read_text())
    assert "ValueError: boom" in rec["failed"]["log"]


def test_undoing_a_breaking_change_clears_the_failure(project):
    build(project)
    (project / "src" / "plate.py").write_text("raise ValueError('boom')\n")
    build(project, "plate")
    (project / "src" / "plate.py").write_text(PLATE)
    check = subprocess.run([sys.executable, "-m", "tule_cli", "build", "--check"], cwd=project,
                           capture_output=True, text=True).stdout
    assert check.strip() == "everything is up to date"
    build(project, "plate")
    rec = json.loads((project / "outputs" / ".build" / "plate.json").read_text())
    assert "failed" not in rec


def test_an_output_that_reads_a_stale_one_is_stale_too(project):
    from tule_cli import build as b

    build(project)
    (project / "lib" / "params.py").write_text("WALL = 7\n")
    rows = {r["name"]: r for r in b.status(str(project))}
    assert rows["plate"]["why"] == "lib/params.py changed"
    assert rows["bom"] == {**rows["bom"], "state": "stale", "why": "outputs/plate is out of date"}


def test_agent_work_is_flagged_when_what_it_was_based_on_changes(project):
    from tule_cli import build as b

    build(project)
    (project / "work" / "suppliers").mkdir(parents=True)
    (project / "work" / "suppliers" / "plate.json").write_text('[{"supplier": "Acme", "price": 12.5}]')
    r = subprocess.run([sys.executable, "-m", "tule_cli", "based-on", "work/suppliers/plate.json", "outputs/plate"],
                       cwd=project, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert b.based_on_status(str(project))[0]["changed"] == []

    (project / "lib" / "params.py").write_text("WALL = 8\n")
    err = build(project)[1]
    assert "check work/suppliers/plate.json again (outputs/plate changed)" in err
    assert b.based_on_status(str(project))[0]["changed"] == ["outputs/plate"]

    b.based_on(str(project), "work/suppliers/plate.json")  # checked: still holds
    assert b.based_on_status(str(project))[0]["changed"] == []
    assert "again (" not in build(project)[1]


def test_agent_work_lives_in_work(project):
    from tule_cli import build as b

    build(project)
    with pytest.raises(b.BuildError, match="kept in work/"):
        b.based_on(str(project), "src/plate.py", ["outputs/plate"])


def test_subsets_dependents_and_skip(project):
    build(project)
    (project / "lib" / "params.py").write_text("WALL = 9\n")
    err = build(project, "pl*")[1]
    assert "plate: building" in err and "bom:" not in err  # a pattern; not what reads it
    (project / "lib" / "params.py").write_text("WALL = 10\n")
    err = build(project, "plate", "--dependents")[1]
    assert "plate: building" in err and "bom: building" in err
    (project / "lib" / "params.py").write_text("WALL = 11\n")
    err = build(project, "--skip", "bom")[1]
    assert "plate: building" in err and "bom: building" not in err and "1 skipped" in err
    code, err = build(project, "nope*")
    assert code == 1 and "no src/nope*.py" in err


def test_check_lists_what_needs_updating(project):
    def check():
        return subprocess.run([sys.executable, "-m", "tule_cli", "build", "--check"], cwd=project,
                              capture_output=True, text=True).stdout

    build(project)
    assert check().strip() == "everything is up to date"
    (project / "lib" / "params.py").write_text("WALL = 7\n")
    out = check()
    assert "plate  out of date  lib/params.py changed  (last run" in out
    assert "bom    out of date  outputs/plate is out of date" in out


def test_skip_leaves_what_it_reads_as_is_and_force_runs_only_the_named(project):
    build(project)
    (project / "lib" / "params.py").write_text("WALL = 7\n")
    (project / "inputs" / "prices.csv").write_text("plate,13.00\n")
    code, err = build(project, "--skip", "plate")
    assert code == 0, err
    assert "bom: built" in err and "plate: building" not in err  # bom reads the plate as it is
    code, err = build(project, "src/bom.py", "--force")  # a path works too
    assert "bom: building (forced)" in err and "plate: building (forced)" not in err


def test_outputs_that_read_each_other_fail(project):
    one = 'from tule import outputs\nopen("outputs/{}/x.txt").read()\nopen(outputs.folder() + "/x.txt", "w").write("x")\n'
    (project / "src" / "a.py").write_text(one.format("b"))
    (project / "src" / "b.py").write_text(one.format("a"))
    code, err = build(project, "a", "b")
    assert code == 1 and "outputs/a and outputs/b read each other (a → b → a)" in err

    # a read b, now b reads a: the old record is no reason to fail
    (project / "src" / "a.py").write_text('from tule import outputs\nopen(outputs.folder() + "/x.txt", "w").write("x")\n')
    (project / "src" / "b.py").write_text('from tule import outputs\nopen(outputs.folder() + "/x.txt", "w").write("x")\n')
    build(project, "a", "b")
    (project / "src" / "a.py").write_text(one.format("b"))
    build(project, "a")
    (project / "src" / "a.py").write_text('from tule import outputs\nopen(outputs.folder() + "/x.txt", "w").write("y")\n')
    (project / "src" / "b.py").write_text(one.format("a"))
    code, err = build(project, "a", "b")
    assert code == 0, err


def test_a_file_changed_while_the_script_ran_stays_out_of_date(project, tmp_path_factory):
    flag = tmp_path_factory.mktemp("flags")
    (project / "src" / "slow.py").write_text(f"""import os, time
from tule import outputs
open("inputs/prices.csv").read()
open({str(flag / 'started')!r}, "w").close()
while not os.path.exists({str(flag / 'go')!r}):
    time.sleep(0.05)
open(outputs.folder() + "/x.txt", "w").write("x")
""")
    env = {k: v for k, v in os.environ.items() if k not in ("TULE_WORKSPACE", "TULE_OUTPUTS")}
    p = subprocess.Popen([sys.executable, "-m", "tule_cli", "build", "slow"], cwd=project, env=env,
                         stderr=subprocess.DEVNULL)
    for _ in range(600):
        if (flag / "started").exists():
            break
        import time
        time.sleep(0.05)
    (project / "inputs" / "prices.csv").write_text("plate,99.00\n")
    (flag / "go").touch()
    assert p.wait(timeout=120) == 0
    err = build(project, "slow")[1]
    assert "slow: building (inputs/prices.csv changed)" in err


def test_a_killed_build_leaves_nothing_behind(project):
    build(project, "plate")
    (project / "outputs" / ".build" / "stage-plate-99999" / "plate").mkdir(parents=True)
    build(project, "plate")
    assert not [n for n in os.listdir(project / "outputs" / ".build") if n.startswith("stage-")]


def test_based_on_ignores_what_is_not_a_file_in_the_project(project):
    import signal

    from tule_cli import build as b

    (project / "work").mkdir()
    (project / "work" / "notes.md").write_text("ok\n")
    (project / "inputs" / "pipe").mkdir()
    os.mkfifo(project / "inputs" / "pipe" / "fifo")  # reading it would never end
    signal.alarm(30)
    try:
        b.based_on(str(project), "work/notes.md", ["inputs/pipe"])
        notes = json.loads((project / "work" / ".based-on.json").read_text())
        notes["work/notes.md"].update({"/": "x", "../..": "x"})  # edited by hand
        notes["../outside"] = {"inputs": "x"}
        (project / "work" / ".based-on.json").write_text(json.dumps(notes))
        assert b.based_on_status(str(project)) == [{"path": "work/notes.md", "sources": ["inputs/pipe"], "changed": []}]
    finally:
        signal.alarm(0)
    with pytest.raises(b.BuildError, match="not in the project"):
        b.based_on(str(project), "work/notes.md", ["../.."])


def test_check_names_and_scripts_that_are_gone(project):
    def check(*names):
        return subprocess.run([sys.executable, "-m", "tule_cli", "build", "--check", *names], cwd=project,
                              capture_output=True, text=True).stdout

    build(project)
    (project / "lib" / "params.py").write_text("WALL = 7\n")
    assert "bom" not in check("plate") and "plate" in check("plate")
    (project / "src" / "bom.py").rename(project / "src" / "cost.py")
    assert "no script    src/bom.py is gone: delete outputs/bom" in check()


def test_a_repo_with_src_is_not_a_project_without_tule_toml(project):
    from tule_cli.build import find_root

    assert find_root(str(project / "src")) == str(project)
    (project / "tule.toml").unlink()
    assert find_root(str(project / "src")) is None

