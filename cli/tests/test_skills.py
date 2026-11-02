"""The agent instructions in skills/: every Python example runs, tule docs serves them, AGENTS.md is current."""

import os
import re
import runpy
import subprocess
import sys

import pytest

from tule_cli import main

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILLS = os.path.join(ROOT, "skills")
DOCS = sorted(f for f in os.listdir(SKILLS) if f.endswith(".md"))


def python_blocks(path):
    return re.findall(r"```python\n(.*?)```", open(path).read(), re.S)


@pytest.mark.parametrize("doc", DOCS)
def test_examples_run(doc, tmp_path):
    """Each doc's Python blocks run in order in one folder that has the example bracket in outputs/."""
    blocks = python_blocks(os.path.join(SKILLS, doc))
    if not blocks:
        pytest.skip("no Python examples")
    # this checkout's tule, not whichever one the venv installed (a worktree's docs must run against its code)
    env = {**os.environ, "TULE_OUTPUTS": str(tmp_path / "outputs"), "PYTHONPATH": os.path.join(ROOT, "python")}
    env.pop("TULE_WORKSPACE", None)
    subprocess.run([sys.executable, os.path.join(ROOT, "examples", "bracket.py")], cwd=tmp_path, env=env,
                   check=True, capture_output=True)
    for i, code in enumerate(blocks):
        r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, f"{doc}, Python block {i + 1}:\n{code}\n{r.stderr}"


def test_docs_are_short_and_titled():
    for doc in DOCS:
        text = open(os.path.join(SKILLS, doc)).read()
        assert text.startswith("# "), doc
        assert len(text) < 5500, f"{doc} is {len(text)} bytes; keep each topic short"


def test_tule_docs(capsys, monkeypatch):
    monkeypatch.setenv("TULE_SKILLS", SKILLS)
    main(["docs"])
    out = capsys.readouterr().out
    for doc in DOCS:
        assert f"  {doc[:-3]} " in out
    assert out.splitlines()[1].split()[0] == "index"
    main(["docs", "modeling"])
    assert "from tule import" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="no topic 'nope'"):
        main(["docs", "nope"])


def test_tule_docs_prints_the_library_reference(capsys, monkeypatch):
    monkeypatch.setenv("TULE_SKILLS", SKILLS)
    main(["docs", "Shape.fillet"])
    out = capsys.readouterr().out
    assert out.startswith("Shape.fillet(edges: Selection, radius: float) -> Shape") and "Round" in out
    main(["docs", "Assembly"])
    out = capsys.readouterr().out
    assert out.startswith("class Assembly(") and "  gaps(below: float = 1.0" in out
    main(["docs", "tule.outputs"])  # "outputs" alone is the topic
    assert "  sequence(name: str" in capsys.readouterr().out
    main(["docs", "tule.fea"])
    assert "  static(" in capsys.readouterr().out


def test_agents_md_is_generated_from_skills():
    build = runpy.run_path(os.path.join(ROOT, "tools", "build_agents_md.py"))["build"]
    with open(os.path.join(ROOT, "templates", "project", "AGENTS.md")) as f:
        assert f.read() == build(), "templates/project/AGENTS.md is stale: run python tools/build_agents_md.py"
