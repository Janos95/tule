You write the morning report of a night of automated tests of Tule, an engineering computer for AI agents. Real coding agents (Codex, Claude) worked through scenarios as a simulated user would ask them, and a reviewer listed findings per run.

The current folder holds the night: each run is a folder `<scenario>-<agent>-<n>/` with run.json (scenario, agent, minutes, the simulated user's verdicts) and findings.json (the reviewer's findings). You may open a run's conversation.md, turn-N.md or machine.md to check a finding.

Write report.md (Markdown) for the developer who builds Tule. Short and plain:
1. One paragraph: how many runs, how many met the user's goal, per agent; anything that broke the harness itself.
2. Findings, merged across runs (the same problem seen in several runs is one finding) and ranked by how much they would bother users and how often they happened. For each: a short title, how often (e.g. "4 of 12 runs, codex and claude"), who should fix it (docs, cli, control, viewer, machine), one or two sentences of what happens with a pointer to one run (`suppliers-codex-1/turn-2.md`), and the fix you suggest. Leave out findings owned by "agent" unless Tule could make them less likely.
3. A table of runs: scenario, agent, minutes, turns, goal met.

Write the file report.md in the current folder, then answer "done".
