# Agent QA

Real coding agents (Codex, Claude Code) use Tule the way a user's agent would, while a
simulated user talks to them, and a reviewer looks for anything odd. Run it overnight;
read `report.md` in the morning.

```sh
python3 qa/run.py                                   # every scenario with both agents, once
python3 qa/run.py --hours 8 --repeat 10             # all night
python3 qa/run.py --scenarios thicker --agents codex --turns 2 --no-image   # one quick run
```

Each run:

1. A fresh guest account (as a first visit makes one) on a local control plane started
   for the night (port 8788, its own state in `runs/<night>/state`; your dev server on 8787
   is not touched). A project, loaded on a machine: a `tule-runtime:dev` container joined to
   it, then set up as the scenario says.
2. The agent gets the opening message with Tule's connector (an API key of the guest). It
   runs on this computer in its own folder, with its own `tule` CLI and sign-in (the
   connector's install command puts them there): Codex in its workspace-write sandbox
   without your config or ChatGPT connectors, Claude Code in its Bash sandbox with only
   this connector.
3. After each turn: screenshots of the viewer and the projects page as the guest sees
   them, whether the "Connect your agent" banner is still there, and the simulated user's
   next message (`prompts/user.md`), until the goal is met or it gives up.
4. The project afterwards (git log, unsaved changes, files, `tule build --check`, scripts),
   files the agent left on this computer, the control plane's log, and a review
   (`prompts/review.md`) with findings: what, how bad, who should fix it.

`runs/<night>/report.md` merges the findings of the night (`prompts/report.md`).

**Turn by turn:** you (or an agent) play the user instead of the simulated one:
`run.py serve` starts the control plane, then `start`, `say`, `wait`, `shell`, `shot` and
`finish` drive one run a message at a time (see the comment above them in `run.py`).

**Scenarios** are in `scenarios.toml`: an opening message as a user would type it (vague
on purpose), a persona, a goal, and how to set the project up. Add the odd things you
notice yourself as new scenarios.

Needs Docker, Node, Chrome, and Codex and/or Claude Code signed in (`codex login`, `claude`
then /login). The simulated user and the reviewer are Claude when its CLI is signed in,
else Codex.

**Not covered:** adding the connector in ChatGPT's or Claude's own pages (sign-in and
clicks), Tule's cloud machines and RunPod (machines are local containers), production
data. Codex sessions appear in your Codex history.
