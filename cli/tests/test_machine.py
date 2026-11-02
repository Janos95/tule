"""tule screenshot / inspect / docs / snapshot (on the machine)."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading

import numpy as np
import pytest
from PIL import Image

from tule_cli import main
from tule_cli.machine import inspect_file, scene
from tule_cli.render import BACKGROUND, render

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def bracket():
    from tule import circle, cylinder, rect

    plate = rect(80, 40, center=True).round(5).extrude(6)
    plate = plate.tag("mount", plate.faces["bottom"])
    boss = circle(9).extrude(12).at(0, 0, 6)
    part = (plate | boss).fillet(boss.edges["bottom"], 2) - cylinder(3.2, 50, center=True)
    return part.tag("load", part.faces.top()).named("bracket", "#4a8")


@pytest.fixture(scope="module")
def bracket_step(tmp_path_factory):
    path = tmp_path_factory.mktemp("bracket") / "bracket.step"
    bracket().export_step(str(path))
    return path


@pytest.fixture
def outputs(tmp_path, monkeypatch, bracket_step):
    """An outputs folder with the bracket as a part (outputs/bracket/bracket.step, no output.json)."""
    d = tmp_path / "outputs"
    (d / "bracket").mkdir(parents=True)
    shutil.copy(bracket_step, d / "bracket" / "bracket.step")
    monkeypatch.setenv("TULE_OUTPUTS", str(d))
    return d


def not_blank(path, min_fraction=0.15):
    a = np.asarray(Image.open(path).convert("RGB")).astype(int)
    bg = np.array([round(255 * c) for c in BACKGROUND])
    return (np.abs(a - bg).sum(-1) > 30).mean() > min_fraction


def test_screenshot_bracket_with_labels(outputs, tmp_path, capsys):
    png = tmp_path / "view.png"
    main(["screenshot", "-o", str(png), "--labels"])  # the newest output
    out = capsys.readouterr().out
    im = Image.open(png)
    assert im.width == 1200 and 600 <= im.height <= 1300
    assert not_blank(png)
    assert "labeled:" in out and "f12" in out and "f0" in out
    assert "not visible from iso: f5, f7, f9, f11" in out  # the back, the bottom


def test_render_report_and_views(outputs):
    parts, overlay, caption = scene(str(outputs / "bracket"))
    assert parts[0].tags == {12: ["load"], 11: ["mount"]}
    img, rep = render(parts, "top", 600, labels=True, caption=caption)
    assert img.width == 600
    assert "f12" in rep["labeled"]
    assert "f11" in rep["hidden"]  # the mount face is underneath
    _, rep = render(parts, "bottom", 600, labels=True)
    assert "f11" in rep["labeled"] and "f12" in rep["hidden"]


def test_screenshot_overlay(outputs, tmp_path, capsys):
    d = outputs / "loads"
    d.mkdir()
    shutil.copy(outputs / "bracket" / "bracket.step", d / "bracket.step")
    (d / "output.json").write_text(json.dumps({
        "version": 1, "kind": "overlay", "file": "bracket.step",
        "faces": {"12": {"role": "load", "label": "500 N"}, "1": {"severity": "warning", "label": "too small"}},
        "arrows": [{"at": [0, 5.5, 18], "dir": [0, 0, -1], "label": "500 N"}],
    }))
    png = tmp_path / "o.png"
    main(["screenshot", "--output", "loads", "-o", str(png)])
    out = capsys.readouterr().out
    assert "labeled: f12, f1" in out  # only the overlay's faces
    a = np.asarray(Image.open(png).convert("RGB")).astype(int)
    orange = (a[..., 0] > 180) & (a[..., 1] > 60) & (a[..., 1] < 160) & (a[..., 2] < 80)
    assert orange.sum() > 500  # the load face


def test_screenshot_stl_sequence_and_errors(outputs, tmp_path, capsys):
    from tule import box, outputs as out, sphere

    seq = out.sequence("blob")
    for i in range(2):
        seq.add(sphere(5 + i) | box(14, 3, 3, center=True), label=f"it {i}", c=1.0 / (i + 1))
    seq.done()
    png = tmp_path / "s.png"
    main(["screenshot", str(outputs / "blob"), "-o", str(png), "--view", "front", "--size", "500"])
    assert Image.open(png).width == 500 and not_blank(png, 0.1)
    with pytest.raises(SystemExit, match="no output 'nope'.*bracket"):
        main(["screenshot", "--output", "nope", "-o", str(png)])
    with pytest.raises(SystemExit, match="unknown view"):
        main(["screenshot", "-o", str(png), "--view", "sideways"])


def test_inspect(outputs, capsys):
    step = str(outputs / "bracket" / "bracket.step")
    (p,) = inspect_file(step)
    assert p["name"] == "bracket" and p["color"] == "#44aa88"
    assert p["tags"] == {"load": ["f12"], "mount": ["f11"]}
    assert len(p["faces"]) == 14 and p["edges"] > 0 and p["valid"]
    assert p["size"] == pytest.approx([80, 40, 18], abs=1e-6)
    main(["inspect", step])
    out = capsys.readouterr().out
    assert "14 faces" in out and "size 80 x 40 x 18" in out
    line = next(x for x in out.splitlines() if x.strip().startswith("f12 "))
    assert "plane" in line and "normal +z" in line and "[load]" in line
    main(["inspect", step, "--json"])
    assert json.loads(capsys.readouterr().out)[step][0]["tags"]["load"] == ["f12"]


def test_snapshot_on_the_machine(tmp_path, monkeypatch, capsys):
    """`tule snapshot` without a login, on a machine, asks the local tule-agent (runtime/local.py)."""
    sys.path.insert(0, os.path.join(ROOT, "runtime"))
    import local

    sock = os.path.join(tempfile.mkdtemp(prefix="tule", dir="/tmp"), "agent.sock")  # short: Unix socket paths are limited

    class FakeWorkspace:
        root = str(tmp_path)
        last_id = "s1"
        calls = []

        def snapshot(self, reason):
            self.calls.append(reason)
            return "s2" if len(self.calls) == 1 else None

    class FakeAgent:
        booted = {"t": "ready"}
        ws = None
        lock = None
        workspace = FakeWorkspace()

    loop = asyncio.new_event_loop()
    task = {}

    async def start():
        FakeAgent.lock = asyncio.Lock()
        task["serve"] = asyncio.ensure_future(local.serve(FakeAgent(), sock))

    async def stop():
        task["serve"].cancel()
        await asyncio.gather(task["serve"], return_exceptions=True)

    threading.Thread(target=loop.run_forever, daemon=True).start()
    asyncio.run_coroutine_threadsafe(start(), loop).result()
    for _ in range(100):
        if os.path.exists(sock):
            break
        threading.Event().wait(0.02)
    monkeypatch.setattr("tule_cli.machine.SOCKET", sock)
    monkeypatch.setenv("TULE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("tule_cli.CONFIG", str(tmp_path / "config" / "config.json"))
    monkeypatch.delenv("TULE_URL", raising=False)
    monkeypatch.delenv("TULE_TOKEN", raising=False)
    monkeypatch.setenv("TULE_WORKSPACE", str(tmp_path))
    main(["snapshot"])
    assert capsys.readouterr().out.strip() == "tule: snapshot s2"
    main(["snapshot"])
    assert "s1 (nothing changed since)" in capsys.readouterr().out
    assert FakeWorkspace.calls == ["manual", "manual"]
    asyncio.run_coroutine_threadsafe(stop(), loop).result()
    loop.call_soon_threadsafe(loop.stop)


def test_motion_frames_and_assembly_placements(tmp_path, monkeypatch):
    from tule import Assembly, box, outputs

    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path))
    base, arm = box(10).named("base", "#4a8"), box(2, 2, 20).named("arm", "#d33")
    with outputs.sequence("swing") as seq:
        for a in (0, 90):
            seq.add([base, arm.rotate(y=a).at(10, 0, 10)], label=f"{a} deg")
    outputs.assembly("pair", Assembly(base, arm.at(30, 0, 0)))
    first, _, cap0 = scene(str(tmp_path / "swing"), 1)
    last, _, cap = scene(str(tmp_path / "swing"))
    hi = lambda ps: np.concatenate([p.positions for p in ps]).max(0)
    assert hi(first)[2] == pytest.approx(30, abs=1e-3)  # standing up
    assert hi(last)[0] == pytest.approx(30, abs=1e-3) and hi(last)[2] == pytest.approx(10, abs=1e-3)  # swung over
    assert "frame 2 of 2 (done), 90 deg" in cap and "frame 1 of 2" in cap0
    parts, _, _ = scene(str(tmp_path / "pair"))
    assert hi(parts)[0] == pytest.approx(32, abs=1e-3)
