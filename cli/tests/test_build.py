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


def build(root, *args, machine="/nonexistent/machine.json"):
    env = {k: v for k, v in os.environ.items() if k not in ("TULE_WORKSPACE", "TULE_OUTPUTS")}
    env["TULE_MACHINE_FILE"] = str(machine)  # off a Tule machine: no build cache
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


def test_scratch_files_in_a_temporary_folder_are_fine(project):
    (project / "src" / "plate.py").write_text(PLATE + """import tempfile, os
with tempfile.TemporaryDirectory() as d:
    open(os.path.join(d, "iso.png"), "w").write("x")
    os.mkdir(os.path.join(d, "sub"))
""")
    code, err = build(project, "plate")
    assert code == 0, err


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
    assert rec["failed"]["error"] == "ValueError: boom"  # what the viewer and --check lead with, not "exit status 1"


def test_check_says_building_while_a_build_runs(project):
    stage = project / "outputs" / ".build" / f"stage-plate-{os.getpid()}"  # a live process's stage folder
    stage.mkdir(parents=True)
    check = subprocess.run([sys.executable, "-m", "tule_cli", "build", "--check"], cwd=project,
                           capture_output=True, text=True).stdout
    assert any(line.split()[:2] == ["plate", "building"] for line in check.splitlines()), check


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


@pytest.fixture
def control(tmp_path_factory):
    """A stand-in for the control plane's blobs and build cache; yields a machine.json."""
    import hashlib
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    blobs, entries = {}, {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, body=b"{}"):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path.endswith("/cache"):
                es = sorted(entries.values(), key=lambda e: -e["at"])
                return self.reply(200, json.dumps({"entries": es}).encode())
            h = path.rsplit("/", 1)[1]
            return self.reply(200, blobs[h]) if h in blobs else self.reply(404)

        def do_POST(self):
            b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith("/blobs/missing"):
                return self.reply(200, json.dumps({"missing": [h for h in b["hashes"] if h not in blobs]}).encode())
            entries[(b["name"], b["key"])] = {**b, "machine": "cpu-1", "at": len(entries)}
            return self.reply(200)

        def do_PUT(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            assert hashlib.sha256(data).hexdigest() == self.path.rsplit("/", 1)[1]
            blobs[self.path.rsplit("/", 1)[1]] = data
            return self.reply(200)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    m = tmp_path_factory.mktemp("etc") / "machine.json"
    m.write_text(json.dumps({"url": f"http://127.0.0.1:{srv.server_port}", "project": "p_test", "token": "t"}))
    yield m
    srv.shutdown()


def test_outputs_built_on_one_machine_are_downloaded_on_another(project, tmp_path_factory, control):
    import shutil

    code, err = build(project, machine=control)
    assert code == 0 and "build cache" not in err
    other = tmp_path_factory.mktemp("other") / "p"  # another machine's copy: the same saved files, no outputs
    shutil.copytree(project, other, ignore=shutil.ignore_patterns("outputs"))
    check = subprocess.run([sys.executable, "-m", "tule_cli", "build", "--check"], cwd=other, capture_output=True, text=True,
                           env={**os.environ, "TULE_MACHINE_FILE": str(control)}).stdout
    assert "plate  out of date  never built (the cache has a matching build from cpu-1: tule build fetches it)" in check
    assert "bom    out of date  never built (the cache has a matching build from cpu-1" in check
    code, err = build(other, machine=control)
    assert code == 0 and "2 fetched" in err and "building" not in err
    assert (other / "outputs" / "bom" / "bom.csv").read_text() == (project / "outputs" / "bom" / "bom.csv").read_text()
    assert "2 up to date" in build(other, machine=control)[1]
    (other / "lib" / "params.py").write_text("WALL = 7\n")  # changed here: run, not downloaded
    code, err = build(other, machine=control)
    assert "plate: building" in err and "2 built" in err



def test_builds_without_the_control_plane(project, tmp_path_factory):
    m = tmp_path_factory.mktemp("etc") / "machine.json"
    m.write_text(json.dumps({"url": "http://127.0.0.1:9", "project": "p_test", "token": "t"}))
    code, err = build(project, machine=m)
    assert code == 0 and "2 built" in err and err.count("build cache") == 1


def test_groups_in_src(project):
    """src/checks/fit.py makes outputs/checks/fit/ (a group in the viewer) and can read other outputs."""
    (project / "src" / "checks").mkdir()
    (project / "src" / "checks" / "fit.py").write_text(
        'from tule import import_step, outputs\n'
        'p = import_step("outputs/plate/plate.step")\n'
        'outputs.part("fit", p.at(0, 0, 10))\n')
    code, err = build(project, "checks/*")
    assert code == 0, err
    assert err.index("plate: built") < err.index("checks/fit: built")
    assert (project / "outputs" / "checks" / "fit" / "fit.step").exists()
    assert (project / "outputs" / ".build" / "checks~fit.json").exists()
    def check():
        return subprocess.run([sys.executable, "-m", "tule_cli", "build", "--check"], cwd=project,
                              capture_output=True, text=True).stdout

    (project / "lib" / "params.py").write_text("WALL = 8\n")  # what it read changed: it is stale
    assert "checks/fit" in check()
    (project / "src" / "checks" / "fit.py").rename(project / "src" / "checks" / "gone.py")
    assert "delete outputs/checks/fit" in check()  # its script is gone


def test_models_too_big_for_the_viewer_are_flagged(tmp_path):
    from tule_cli.build import models_mb
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.step").write_bytes(b"0" * (3 << 20))
    (tmp_path / "a" / "notes.md").write_bytes(b"0" * (5 << 20))
    assert round(models_mb(str(tmp_path / "a"))) == 3
    (tmp_path / "a" / "output.json").write_text('{"kind": "sequence"}')
    assert models_mb(str(tmp_path / "a")) == 0  # one frame at a time


def test_an_opencascade_error_says_where_in_the_project(project):
    (project / "src" / "edge.py").write_text(
        "from OCP.BRep import BRep_Builder\nfrom OCP.BRepAdaptor import BRepAdaptor_Curve\nfrom OCP.TopoDS import TopoDS_Edge\n"
        "e = TopoDS_Edge()\nBRep_Builder().MakeEdge(e)\nBRepAdaptor_Curve(e)\n")
    code, err = build(project, "edge")
    assert code and "OpenCascade failed at src/edge.py:6 (Standard_Failure: BRepAdaptor_Curve::No geometry)" in err


def test_a_module_imported_from_its_cache_is_still_read(project):
    import py_compile
    py_compile.compile(str(project / "lib" / "params.py"), cfile=str(project / "lib" / "__pycache__" / f"params.cpython-{sys.version_info.major}{sys.version_info.minor}.pyc"))
    assert build(project, "plate")[0] == 0
    rec = json.loads((project / "outputs" / ".build" / "plate.json").read_text())
    assert "lib/params.py" in rec["reads"]
