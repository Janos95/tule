"""Writes templates/project/AGENTS.md (the instructions in every new project) from the core skills.

    python tools/build_agents_md.py

skills/ is the single source: the same files are `tule docs <topic>` on the
machine and the connector's guide(topic). Edit them, not AGENTS.md
(cli/tests/test_skills.py checks that it is current).
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = ["index", "loop", "modeling", "outputs"]
OUT = os.path.join(ROOT, "templates", "project", "AGENTS.md")


def build() -> str:
    parts = []
    for name in CORE:
        with open(os.path.join(ROOT, "skills", name + ".md")) as f:
            parts.append(f.read().strip() + "\n")
    head = (f"<!-- Generated from skills/{{{','.join(CORE)}}}.md by tools/build_agents_md.py; edit those. "
            "More: tule docs <topic>. -->\n\n")
    return head + "\n---\n\n".join(parts)


if __name__ == "__main__":
    text = build()
    with open(OUT, "w") as f:
        f.write(text)
    print(f"wrote {OUT} ({len(text)} bytes)")
