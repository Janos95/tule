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
