# Tule — design

Tule is an engineering computer for your own AI agent. Each project gets an
isolated container with OpenCascade, Tule's Python modeling API and
engineering libraries (DFM, topology optimization, later FEA) preinstalled;
your agent works in it over SSH, the project's state is a git repository, and
a web viewer shows everything the project renders, live.

It is the successor to sprig and reuses the viewer, editor and project UI of
[genau](https://github.com/Janos95/genau)'s web playground. genau's mesh
kernel is replaced by OpenCascade (OCCT), and all computation moves from the
browser to the server.

Status: draft. Open decisions are listed at the end, each with the default
assumed here.

## Why

Agents (Claude Code, Codex, Cursor, …) already have shells and persistent
machines. What they lack for engineering work, and what Tule provides:

1. **An engineering stack, ready and pinned.** OCCT, the `tule` API, DFM,
   topology optimization, meshers, solvers, in one image, the same for every
   run, natively (`pip install tule`) and in CI.
2. **Compute.** Bigger CPU containers than an agent's sandbox, and GPU
   machines on demand.
3. **Eyes and a window.** A viewer the human watches while the agent works,
   and renders (with face labels) the agent can look at.
4. **Projects that last.** A git repository per project: history, branches
   for design variants, diffs, push to GitHub.

Tule does not run or bill for an agent. Users bring their own.

## Principles

1. **The container is a general computer.** The agent can do anything inside
   (root, `pip install`, background processes). Engineering capability lives
   in Python libraries, not in special tool calls, so it composes: a
   parameter sweep can call topology optimization and DFM in a loop.
2. **The model is the interface.** Faces carry names and tags that survive
   modeling operations; analysis libraries take tagged shapes and report in
   terms of those faces.
3. **One way to write things.** Immutable values and operators, as in genau.
   No builder contexts, no workplane stack, no string selectors.
4. **Agent-first errors.** A failure says what failed, where, and what would
   work.
5. **Shaped like RunPod.** One Tule connector: an MCP server for the control
   plane (projects, machines, keys, usage), SSH and the `tule` CLI for the
   work, and skills that teach the agent both. One sign-in covers all of it.
   The instructions also ship in every project (`AGENTS.md`) and in the
   container (`tule docs`), so agents without the plugin work too.

## Architecture

```
  your machine                         Cloudflare (control plane)
 ┌───────────────────┐               ┌─────────────────────────────────────────┐
 │ agent + `tule` CLI│──WSS (SSH,────▶ Worker: auth, CLI API, viewer assets,    │
 │ git               │   git, API)   │         tunnel endpoint                 │
 └───────────────────┘               │   │                                     │
 ┌───────────────────┐               │   ▼                                     │
 │ browser: viewer,  │◀──WSS─────────│ Durable Object "Project" (one each)     │
 │ editor, projects  │               │   - routes SSH/git/viewer streams       │
 └───────────────────┘               │   - provisions + sleeps the runtime     │
                                     │   - run history, frames index           │
                                     │ R2: git repos, frames, exports, renders │
                                     │ Durable Object "Accounts": users, keys, │
                                     │   projects, quotas                      │
                                     └───────────────▲─────────────────────────┘
                                                     │ WSS, dialed OUT by the runtime
                                  ┌──────────────────┴───────────────────┐
                                  │ runtime container (one per project)  │
                                  │  tule-agent · sshd · git checkout    │
                                  │  Python, OCP/OCCT, tule, numpy/scipy │
                                  │  backend: Cloudflare Containers (CPU)│
                                  │        or RunPod / others (GPU)      │
                                  └──────────────────────────────────────┘
```

### The runtime dials out

Every runtime container runs `tule-agent` at boot. It opens a WebSocket
*out* to its project's Durable Object and multiplexes everything over it:
SSH sessions, git, `show()` frames for the viewer, logs and heartbeats.
Consequences:

- SSH, the viewer, git and the CLI work the same on every backend. No
  inbound ports, no per-provider networking.
- Adding a compute backend means writing only its provisioning call: start
  our image with a one-time token, wait for the agent to connect.

### Backends

- **Cloudflare Containers (default, CPU).** One instance per project, bound
  to its Durable Object; starts on demand, sleeps when idle. Up to 4 vCPU /
  12 GiB / 20 GB disk per instance (custom instance types), no GPU. Enough
  for modeling, DFM and moderate topology optimization.
- **GPU backends (RunPod first; Modal, others later).** The Project Durable
  Object calls the provider's API to start the CUDA image. Two shapes:
  1. *Whole-session machine*: `tule up --gpu a100` moves the project's
     computer to a GPU box. Same SSH flow. Built first.
  2. *GPU jobs from a CPU session*: `tule.remote(gpu="A100")(fn)(...)` runs
     one function on a GPU worker with the same image and repo commit,
     streaming progress to the viewer. Built second.
- **Images.** One Dockerfile, two bases (CPU slim, CUDA runtime), the same
  stack on top. Pushed to Cloudflare's registry and to GHCR.

### Project state

- **A project is a git repository.** The canonical copy is a bare repo in R2.
  A runtime restores it at boot and pushes on every commit; the CLI and any
  git client use the project as a remote over the tunnel (`git push tule`),
  and users can add GitHub as another remote.
- **Commits:** explicit commits by the user or agent; each run also records
  a snapshot commit on a hidden ref, so nothing is lost and main history
  stays clean.
- **Outputs stay out of git.** Frames, renders, STEP files and reports go to
  R2, keyed by commit and run.
- **`tule.toml`** in the repo: entry point, parameters, materials, process
  targets, extra pip packages (reinstalled at boot; the heavy stack is in the
  image).
- **New projects** start from a template that includes `AGENTS.md` and
  `CLAUDE.md` with the agent instructions.

### Isolation and quotas

- One container per project; full freedom inside; isolated from other users.
- Outbound network: package registries (PyPI, npm, GitHub) by default, a
  per-project allowlist for more; no open internet.
- Per-user quotas: container-hours, concurrent containers, GPU spend.
- Sign-in with GitHub; SSH keys per user (added with `tule keys add`), and
  project tokens for CI.

## Interfaces

Modeled on RunPod's connector: the MCP manages resources, the work happens
over SSH, and skills route the agent between the two. A user adds Tule once
(plugin or connector URL) and signs in with GitHub; the agent does the rest of
the setup itself (installing the CLI, adding its SSH key), guided by the
skills, the way RunPod's skills walk an agent through pod setup.

### MCP server (control plane)

Hosted at `/mcp`, Streamable HTTP. Auth: OAuth (sign in with GitHub), or a
Tule API key as a Bearer header; the same key also authenticates the CLI.
Tools, grouped by resource:

- **Projects:** list, get, create (from the template), fork, rename, delete.
- **Machines:** start, stop, resize (`cpu-1` … `cpu-4`, `gpu-<type>`), status,
  stream logs; connection info for SSH.
- **GPU jobs:** run, status, stream, cancel (the serverless GPU worker).
- **Keys:** list, add, remove SSH keys; create and revoke API keys.
- **Runs and history:** list runs and commits, with viewer links.
- **Usage and budget:** container minutes, GPU spend, caps.

No exec or file tools: like RunPod, the MCP never carries the work.

### `tule` CLI (on the agent's machine)

```
tule login                     # GitHub sign-in
tule keys add ~/.ssh/id_ed25519.pub
tule new bracket               # create a project (from the template), clone it
tule up [--gpu a100]           # start / resize the project's computer
tule ssh [cmd]                 # shell or command in the container (also: plain ssh via ProxyCommand)
tule run [file]                # run the entry point remotely, stream output and frames
tule screenshot -o view.png [--labels] [--view iso] [--hide part]
tule open                      # open the viewer for this project
tule down | ls | logs
```

`tule ssh --config` writes an `~/.ssh/config` entry with
`ProxyCommand tule proxy %h`, so plain `ssh tule-bracket` and
`git push tule` work. Agents in chat sandboxes can use the same CLI when the
Tule domain is on their network allowlist (the tunnel is HTTPS), or when the
chat is linked to the user's computer; otherwise a chat agent can manage
projects through the MCP but not work inside them (the same limit as
RunPod).

### Inside the container

- `python main.py` / `tule run`: run the project; every `show()` streams to
  the viewer.
- `tule screenshot`, `tule docs [topic]`, `tule inspect <file>:<shape>`.
- The engineering libraries: `tule` (modeling), `tule.dfm`, `tule.topopt`,
  later `tule.fea`.

### Skills and the Claude plugin

The agent instructions (Tule's equivalent of RunPod's skills) are written
once and shipped three ways: as a Claude plugin (the MCP connector plus
skills), in every project as `AGENTS.md`/`CLAUDE.md`, and in the container as
`tule docs`. Content:

- a router (start here): when to use the MCP, when the CLI and SSH;
- setup: install the CLI, sign in, add an SSH key, connect;
- the modeling API and its style, selection, tags, parameters;
- the loop: edit → `tule run` → `tule screenshot` → inspect → iterate,
  branches for variants;
- DFM and topology optimization: how to set up loads, fixed faces, processes;
- OCCT pitfalls (fillets, tolerances, sewing) and how errors read;
- golden paths: tested end-to-end examples ("bracket from load cases",
  "make this part machinable on a 3-axis mill").

### GPU budget

Two ways to reach a GPU, neither of which gives Tule a full RunPod key:

1. **Serverless GPU worker (jobs).** Tule's GPU solvers are published as a
   RunPod Hub worker. The user deploys it into their own RunPod account in
   one click and stores a key restricted to that endpoint in Tule;
   `tule.remote(gpu=..., max_cost=...)` runs jobs on it per second, scaling
   to zero. A leaked key can only run Tule jobs.
2. **The agent as glue (whole GPU machine).** An agent with both the Tule and
   RunPod connectors asks Tule for a one-time join token, creates a pod from
   Tule's CUDA image with the user's RunPod connector, and the pod's
   `tule-agent` dials in and becomes the project's machine. Tule never sees
   RunPod credentials.

In both cases the RunPod prepaid balance is the hard cap (pods stop at $0);
Tule adds per-job budgets and idle shutdown.

## Viewer

genau's page, rewired: it renders frames streamed from the project's runtime
instead of running Python in the browser.

- Kept: viewer, editor drawer, project picker, Parts panel, Download, layout
  and tests.
- Frames per part: positions, normals (from the surfaces), indices, a B-rep
  face id per triangle, B-rep edges as polylines with ids, and metadata
  (name, color, tags, bounds).
- New: wireframe of real CAD edges; hover/click a face or edge to see its id
  and tags and copy a selector; STEP/3MF download; parameter sliders; a runs
  panel (live progress, history by commit); branch comparison side by side;
  DFM findings colored on faces.
- Editing in the page commits to the project like any other client.

## Python API (`tule`)

A thin, opinionated layer over OCP (no build123d dependency; build123d's
source and tests are a reference for OCCT edge cases).

### Values

- `Shape`: an immutable solid, compound or assembly part, with tags and
  provenance.
- `Profile`: a planar region with 2D Booleans, `round`, `chamfer` and
  transforms, as in genau.
- `Path`: a wire for sweeps (lines, arcs, splines, helices).
- `Faces`, `Edges`, `Vertices`: ordered selections that filter further.

### Modeling

```python
from tule import *

plate = rect(80, 40, center=True).round(5).extrude(6)
boss  = circle(9).extrude(12).at(0, 0, 6)
part  = (plate | boss).fillet(boss.edges.bottom, 2)      # provenance survives the union
part -= cylinder(3.2, 50, center=True)
part  = part.shell(part.faces.top(), 1.5)
show(part.named("bracket", "#4a8"))
```

- Primitives: `box`, `cube`, `sphere`, `cylinder`, `cone`, `torus`, `wedge`,
  `polyhedron`.
- Profiles: `polygon` (`(x, y, r)` rounds a corner), `rect`, `circle`,
  `ellipse`, `regular_polygon`, `slot`, `text`; `| - & ^`.
- Sweeps: `extrude` (`twist`, `scale`, `draft`), `revolve`, `loft`,
  `sweep(profile, path)`.
- Booleans: `| + - & ^`, `union`, `difference`, `intersection`.
- Transforms: `at`/`translate`, `rotate`, `scale`, `mirror`, `transform`.
- Features: `fillet`, `chamfer`, `shell`, `offset`, `draft`, `split`,
  `linear_pattern`, `polar_pattern`.
- Parts: `named(name, color)`, `Assembly(*children)`.
- Measure: `volume`, `area`, `bounds`, `center_of_mass`, `inertia`,
  `mass(material)`, `distance`.
- I/O: `export_step`, `export_stl`, `export_3mf`, `export_gltf`,
  `import_step`.
- `show(*shapes, reference=None)`, frames and animations as in genau.

### Selection, tags, parameters

- **Queries:** `s.faces.planar().normal(+Z)`, `.cylindrical(r=3.2)`,
  `.top()`, `s.edges.parallel(Z).on(face)`, `.longest()`, `.within(box)`,
  `.convex()` / `.concave()`.
- **Provenance:** feature results know their faces (`boss.faces.side`,
  `ex.faces.cap_end`); OCCT's builder history (`Modified`, `Generated`,
  `IsDeleted`) carries them through later operations, so
  `part.faces[boss.faces.side]` finds the faces after a union.
- **Tags:** `plate.tag(top="load")`, `part.faces["load"]`; they flow with
  provenance and are what the analysis libraries read.
- **Parameters:** `param("wall", 2.0, min=1.0, max=5.0, unit="mm")`; sliders
  in the viewer, `tule run --param wall=2.5`, sweeps.

### Errors and caching

- `TuleError` with structured fields (`op`, `faces`, `edges`, `hint`, data
  such as `max_radius`); OCCT exceptions never surface unwrapped.
- Operations cached by a hash of their inputs within a container, so small
  edits rerun fast.

## Engineering libraries

- **`tule.dfm`** v0: CNC 3-axis (tool access and undercuts along a setup
  axis, internal corner radius vs tool, pocket depth ratio, thin walls) and
  FDM (overhangs, minimum wall, bridging, bed contact). Findings carry face
  ids, tags, severity and values; the viewer colors them.
- **`tule.topopt`** v0: SIMP on a hex grid (scipy sparse / scikit-fem);
  design domain, non-design regions, fixed faces and loads from tags; result
  as a smoothed isosurface `Shape` plus compliance history, streamed as
  frames. GPU solver later, through `tule.remote`.
- **Later:** `tule.fea` (linear static, gmsh + scikit-fem), cost estimates,
  drawings.

## Repository layout

```
tule/
  python/tule/        modeling API + dfm, topopt (pip package)
  python/tests/
  cli/                the `tule` CLI (Python, pip package)
  runtime/            Dockerfiles (cpu, cuda), tule-agent, sshd config
  control/            Worker + Durable Objects (from genau web/mcp-relay)
  web/                the viewer page (from genau web/src, minus Pyodide/wasm)
  templates/project/  new-project template with AGENTS.md, CLAUDE.md, tule.toml
  skills/             agent instructions (source for AGENTS.md, tule docs, plugin)
  examples/
```

## From genau

- Reused: `web/src` (viewer, editor, projects, picker, parts panel, drawer),
  the relay's Durable Object and WebSocket plumbing, the e2e test approach.
- Rewritten: the page's worker (no Pyodide), the runner (moves into the
  container), viewer normals and wireframe inputs, examples, API docs.
- Dropped: `rust/`, `proofs/`, `tools/`, `bench/`, the wasm builds,
  coi-serviceworker, the MCP relay's agent tools and stand-in, exactness docs.

## Milestones

1. **Spike: the round trip.** CPU runtime image with OCP and `tule-agent`; a
   Cloudflare Container per project that dials out; `tule ssh` and
   `tule run` through the tunnel; a filleted box appears in the copied
   viewer. Measure cold start and image size.
2. **API v0** (native, pytest): primitives, profiles, sweeps, Booleans,
   transforms, fillet/chamfer/shell, queries, provenance, tags, parameters,
   measurements, STEP/STL export.
3. **Projects:** git in R2, `git push tule`, snapshot refs, template with
   `AGENTS.md`, sign-in, keys, quotas.
4. **Viewer:** frames with faces and edges, picking, parts, runs panel,
   STEP download.
5. **Connector:** MCP server with OAuth and API keys, skills, `tule docs`,
   golden paths, the Claude plugin.
6. **Engineering libraries:** `tule.dfm` v0, `tule.topopt` v0.
7. **GPU:** the serverless worker for `tule.remote`, then pods joined by the
   agent.
8. **Deploy** and a live smoke test.

## Open decisions (defaults assumed above)

| Decision | Default |
|---|---|
| Domain | `*.workers.dev` until one is chosen |
| Sign-in | GitHub OAuth |
| Default container size | 1 vCPU / 6 GiB, up to 4 vCPU / 12 GiB |
| Outbound network | package registries + per-project allowlist |
| First GPU backend | RunPod |
| Python version | 3.12 (match the OCP wheels) |
| MCP server | control plane only, as RunPod's |
| Project visibility | private by default, shareable read-only links |
