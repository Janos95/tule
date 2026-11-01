# Tule — design

Tule is an engineering computer for your own AI agent. Each project gets an
isolated machine with OpenCascade, Tule's Python modeling API and engineering
libraries (DFM, topology optimization, later FEA) preinstalled. Your agent
manages projects through the Tule connector and works on the machine over
SSH; everything in the project's `/workspace` is saved; a web viewer shows
the project's outputs, live.

It is the successor to sprig and reuses the viewer, editor and project UI of
[genau](https://github.com/Janos95/genau)'s web playground. genau's mesh
kernel is replaced by OpenCascade (OCCT), and all computation moves from the
browser to the server.

Status: draft. Open decisions are listed at the end, each with the default
assumed here.

## Why

Agents (Claude Code, Codex, Cursor, Claude and ChatGPT chats) already have
shells and persistent machines. What they lack for engineering work, and what
Tule provides:

1. **An engineering stack, ready and pinned.** OCCT, the `tule` API, DFM,
   topology optimization, meshers, solvers, in one image, the same for every
   run, natively (`pip install tule`) and in CI.
2. **Compute.** Bigger CPU machines than an agent's sandbox, and GPU machines
   on demand (CUDA for anything: optimization, simulation, ML, rendering).
3. **Eyes and a window.** A viewer the human watches while the agent works,
   and renders (with face labels) the agent can look at.
4. **Projects that last.** `/workspace` is saved continuously, with git
   inside for code history.

Tule does not run or bill for an agent. Users bring their own.

## Principles

1. **The machine is a general computer.** The agent can do anything inside
   (root, `pip install`, background processes). Engineering capability lives
   in Python libraries, not in special tool calls, so it composes.
2. **Shaped like RunPod.** One Tule connector: an MCP server for the control
   plane (projects, machines, keys, usage), SSH and the `tule` CLI for the
   work, and skills that teach the agent both. One sign-in covers all of it.
3. **Files are the interface between the work and the viewer.** The viewer
   renders what is in `/workspace/outputs/`, described by plain JSON. It
   knows nothing about how the geometry was made.
4. **The model is the interface to analysis.** Faces carry names and tags
   that survive modeling operations; analysis libraries take tagged shapes
   and report in terms of those faces.
5. **One way to write things.** Immutable values and operators, as in genau.
   No builder contexts, no workplane stack, no string selectors.
6. **Agent-first errors.** A failure says what failed, where, and what would
   work.

## User journey

1. **Sign in** on the Tule site with Google (GitHub as an alternative).
2. **Add the connector** to the agent: in Claude or ChatGPT as a custom
   connector (`https://<tule>/mcp`), in Claude Code as a remote MCP server or
   the Tule plugin, in Codex/Cursor as a remote MCP server.
3. **Link the agent to the account.** The first time the agent uses the
   connector, the app opens Tule's sign-in page (MCP OAuth): the user signs
   in with Google and approves "Allow Claude to manage your Tule projects".
   The agent's CLI is linked the same way when the agent first runs
   `tule login` (device code: the agent shows a link and code, the user
   approves it in the browser). No keys are copied by hand.
4. **Connect RunPod (optional)** in Tule's settings: paste a RunPod API key,
   Tule validates it, shows the balance, and the user sets a monthly GPU cap.
5. **Create projects** on the site or by asking the agent ("start a Tule
   project for a camera bracket"); both call the same control plane.

Then the agent works: it starts the project's machine, connects over SSH,
models, writes results to `outputs/`, and the user watches in the viewer.

## Architecture

```
  agent side                           Cloudflare (control plane)
 ┌───────────────────┐               ┌─────────────────────────────────────────┐
 │ agent             │──MCP (HTTPS)──▶ Worker: auth (Google OAuth, MCP OAuth,  │
 │ `tule` CLI / ssh  │──WSS tunnel───▶   device codes), MCP, CLI API,          │
 └───────────────────┘               │   viewer assets, tunnel endpoint        │
 ┌───────────────────┐               │   │                                     │
 │ browser: viewer,  │◀──WSS─────────│ Durable Object "Project" (one each)     │
 │ files, settings   │               │   - owns the project's machine          │
 └───────────────────┘               │   - routes SSH / viewer / sync streams  │
                                     │   - workspace lock, snapshots index     │
                                     │   - usage and budget accounting         │
                                     │ Durable Object "Accounts": users,       │
                                     │   connections (RunPod key, encrypted),  │
                                     │   API keys, SSH keys, projects          │
                                     │ R2: workspace snapshots (by content)    │
                                     └───────────────▲─────────────────────────┘
                                                     │ WSS, dialed OUT by the machine
                                  ┌──────────────────┴───────────────────┐
                                  │ project machine (one at a time)      │
                                  │  tule-agent · sshd · /workspace      │
                                  │  Python, OCP/OCCT, tule, numpy/scipy │
                                  │  backend: Cloudflare Containers (CPU)│
                                  │        or RunPod pod (GPU)           │
                                  └──────────────────────────────────────┘
```

### Pluggable backends

The Project Durable Object treats compute providers through one small
interface:

```ts
interface Backend {
  provision(spec: MachineSpec, joinToken: string): Promise<MachineHandle>; // start our image
  status(h: MachineHandle): Promise<"starting" | "running" | "stopped" | "failed">;
  stop(h: MachineHandle): Promise<void>;
  rate(spec: MachineSpec): number;  // $/hour, for budgets
}
```

- **Cloudflare** (`cpu-1` … `cpu-4`): a Container instance bound to the
  Project Durable Object, started with the join token in its environment.
  Paid by Tule's Cloudflare account; users get a CPU quota.
- **RunPod** (`gpu-<type>`): `POST` a pod to RunPod's REST API with the user's
  stored key, image `ghcr.io/janos95/tule-runtime:cuda`, the GPU type, and the
  join token in its environment. Paid from the user's RunPod balance.
- Later backends (Modal, Lambda, a user's own box) implement the same four
  calls.

Every machine runs the same entrypoint, so nothing above the backend cares
where it runs:

1. `tule-agent` starts and restores `/workspace` from R2 (presigned URLs
   from the Project Durable Object; no storage credentials in the machine).
2. It dials a WebSocket *out* to the Project Durable Object with the join
   token, and from then on carries SSH sessions, workspace sync, output
   change events, logs and heartbeats over it. No inbound ports.
3. It watches `/workspace` and syncs changes back (content-addressed, so
   only new bytes move), and tells the viewer when `outputs/` changes.
4. On stop it takes a final snapshot.

**Switching machines** (`tule up --gpu rtx4090` or the `machines.resize`
tool): the Project Durable Object asks the current machine to snapshot and
stop, provisions the new one, and routes SSH there once its agent dials in.
The agent sees one project whose machine got bigger.

**Shutdown and budgets:** the Durable Object stops a machine after an idle
timeout (no SSH sessions, no running processes it was told about), when a
per-session budget is reached, or when the monthly cap is hit. RunPod also
stops pods when the balance reaches $0, so the prepaid balance is a hard
limit.

**GPU functions from a CPU session** (later): `tule.remote(gpu="rtx4090",
max_cost=1.0)(fn)(...)` runs one function on a short-lived GPU machine with
the same image and a copy of the workspace, and writes its results back into
`/workspace`. Same backend interface; the CPU machine stays the owner of the
workspace.

**RunPod credentials:** the key is encrypted at rest in the Accounts Durable
Object (key from a Worker secret), used only by the control plane to create,
query and stop pods, and never passed into a machine. Creating pods needs a
key with write access to RunPod's API; the settings page says so, suggests a
dedicated key, and shows the user's cap. Users who prefer not to store a key
can let their agent create the pod with their own RunPod connector, passing
a join token from Tule; the pod then joins the project the same way.

### The workspace

Like RunPod's `/workspace`: the only directory that persists. The rest of the
machine is rebuilt from the image every boot.

- **Everything under `/workspace` is the project:** code, uploaded data
  (supplier STEP files, load tables, material sheets, scans), notes, results,
  a virtualenv if the agent wants one.
- **Saved continuously** to R2 as content-addressed files: on change (debounced),
  periodically, on stop, and on `tule snapshot`. Unchanged files are never
  uploaded twice, so snapshots are cheap; the project page can browse and
  restore any snapshot.
- **One writer at a time:** the Project Durable Object holds the workspace
  lock; the machine that owns it syncs, GPU functions get copies.
- **Git inside:** `/workspace` is a git repository for the code, so there are
  diffs, branches and readable history, and it can be pushed to GitHub.
  Snapshots catch everything git does not, so nothing is lost even if the
  agent never commits.
- **`tule.toml`:** extra packages to install at boot (the heavy stack is in
  the image), entry point, parameters.
- **New projects** start from a template: `AGENTS.md`/`CLAUDE.md` with the
  agent instructions, `tule.toml`, `main.py`, empty `outputs/`.

### Outputs

The viewer renders `/workspace/outputs/`: one folder per output, holding
geometry files and an optional `output.json`.

```
outputs/
  bracket/
    bracket.step                  # no JSON needed: one file is one part
  gripper/
    output.json                   # assembly: parts, names, colors, placement
    base.step  finger_l.step  finger_r.step
  bracket-topopt/
    output.json                   # sequence: frames + values -> slider and plot
    iter_000.stl … iter_019.stl
```

```json
{
  "version": 1,
  "kind": "sequence",
  "title": "Topology optimization, volume fraction 0.3",
  "frames": [
    {"file": "iter_000.stl", "label": "iteration 0", "values": {"compliance": 41.2}},
    {"file": "iter_001.stl", "label": "iteration 1", "values": {"compliance": 33.8}}
  ],
  "status": "running"
}
```

- **Kinds in v1:** `part` (one file), `assembly` (several files with names,
  colors, optional transforms; shown in the Parts panel), `sequence` (ordered
  frames with labels and values; slider, play, a plot of the values;
  `"status": "running"` means more frames are coming). Later: `overlay`
  (per-face or per-vertex data on a part, for DFM findings or stress) and
  `variants` (side by side).
- **The JSON is optional.** Without it the viewer infers the kind: one file is
  a part, several an assembly, numbered files a sequence.
- **Formats:** STEP (B-rep) and STL, 3MF, OBJ, glTF (meshes).
- **Names, colors, tags travel in the file.** `export_step` writes part names,
  colors and tags (as named faces), so the viewer shows `f14 · load` without a
  sidecar.
- **Atomic writes:** write to a temporary name and rename; update
  `output.json` last. The viewer never sees half a file.
- **Live:** the machine's watcher reports changes, tessellates new files
  (cached by content hash) and the viewer updates; a running sequence grows
  frame by frame.
- **History for free:** outputs are part of `/workspace`, so the viewer can
  show any output as of any snapshot or commit.
- `tule.outputs` has small helpers that write these folders; nothing requires
  them.

### Isolation and quotas

- One machine per project; full freedom inside; isolated from other users.
- Outbound network: package registries (PyPI, npm, GitHub) by default, a
  per-project allowlist for more; no open internet.
- Quotas: CPU machine-hours per user (Tule pays), GPU spend against the user's
  cap (the user's RunPod pays), concurrent machines.

## Interfaces

Modeled on RunPod's connector: the MCP manages resources, the work happens
over SSH, and skills route the agent between the two.

### MCP server (control plane)

Hosted at `/mcp`, Streamable HTTP. Auth: MCP OAuth (the user signs in with
Google and approves the agent), or a Tule API key as a Bearer header. Tools,
grouped by resource:

- **Projects:** list, get, create (from the template), fork, rename, delete.
- **Machines:** start, stop, resize (`cpu-1` … `cpu-4`, `gpu-<type>`), status,
  logs; connection info for SSH; a join token for a machine the agent
  provisions itself.
- **Snapshots and outputs:** list snapshots, restore; list outputs with viewer
  links.
- **Keys:** SSH keys (list, add, remove), API keys (create, revoke).
- **Usage:** machine hours, GPU spend, caps.

No exec or file tools: like RunPod's, the MCP never carries the work.

### `tule` CLI (on the agent's machine)

```
tule login                     # device code: approve in the browser
tule keys add ~/.ssh/id_ed25519.pub
tule new bracket               # create a project from the template
tule up [--gpu rtx4090]        # start / resize the project's machine
tule ssh [cmd]                 # shell or command on the machine (also: plain ssh via ProxyCommand)
tule run [file]                # run the entry point on the machine, stream output
tule sync [--pull]             # copy /workspace to or from a local folder
tule screenshot -o view.png [--output bracket] [--labels] [--view iso]
tule open                      # open the viewer for this project
tule snapshot | down | ls | logs
```

`tule ssh --config` writes an `~/.ssh/config` entry with
`ProxyCommand tule proxy %h`, so plain `ssh tule-bracket` works. Agents in
chat sandboxes can use the same CLI when the Tule domain is on their network
allowlist (the tunnel is HTTPS), or when the chat is linked to the user's
computer; otherwise a chat agent can manage projects through the MCP but not
work inside them (the same limit as RunPod).

### On the machine

- `python main.py`, any script, any tool; results go to `outputs/`.
- `tule screenshot`, `tule docs [topic]`, `tule inspect <file>`.
- The libraries: `tule` (modeling), `tule.dfm`, `tule.topopt`, `tule.outputs`,
  later `tule.fea`.

### Skills and the Claude plugin

The agent instructions are written once and shipped three ways: as a Claude
plugin (connector plus skills), in every project as `AGENTS.md`/`CLAUDE.md`,
and on the machine as `tule docs`. Content:

- a router (start here): when to use the MCP, when the CLI and SSH;
- setup: `tule login`, SSH key, connect, machines and GPUs, budgets;
- the modeling API and its style, selection, tags, parameters;
- the workspace and `outputs/` conventions;
- the loop: edit → run → write outputs → `tule screenshot` → inspect →
  iterate, branches for variants;
- DFM and topology optimization: loads, fixed faces, processes;
- OCCT pitfalls (fillets, tolerances, sewing) and how errors read;
- golden paths: tested end-to-end examples ("bracket from load cases",
  "make this part machinable on a 3-axis mill", "run a study on a GPU").

## Viewer

genau's page, rewired as a renderer of `outputs/`.

- Kept: viewer, project picker, Parts panel, Download, layout and tests.
- Per part, from the machine's tessellation: positions, normals (from the
  surfaces), indices, a B-rep face id per triangle, B-rep edges as polylines
  with ids, and metadata (name, color, tags, bounds).
- New: the outputs list; sequence slider and value plot; wireframe of real
  CAD edges; hover/click a face or edge to see its id and tags and copy a
  selector; a files browser for `/workspace`; snapshot history (open any
  output as of any snapshot); STEP/3MF download; settings (RunPod, caps, keys);
  usage.
- The code editor stays for small edits; saving writes to `/workspace`.

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
part.named("bracket", "#4a8").export_step("outputs/bracket/bracket.step")
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
- I/O: `export_step` (names, colors, tags), `export_stl`, `export_3mf`,
  `export_gltf`, `import_step`.

### Selection, tags, parameters

- **Queries:** `s.faces.planar().normal(+Z)`, `.cylindrical(r=3.2)`,
  `.top()`, `s.edges.parallel(Z).on(face)`, `.longest()`, `.within(box)`,
  `.convex()` / `.concave()`.
- **Provenance:** feature results know their faces (`boss.faces.side`,
  `ex.faces.cap_end`); OCCT's builder history (`Modified`, `Generated`,
  `IsDeleted`) carries them through later operations, so
  `part.faces[boss.faces.side]` finds the faces after a union.
- **Tags:** `plate.tag(top="load")`, `part.faces["load"]`; they flow with
  provenance, are written into STEP files, and are what the analysis
  libraries read.
- **Parameters:** `param("wall", 2.0, min=1.0, max=5.0, unit="mm")`, set from
  `tule.toml` or `tule run --param wall=2.5`.

### Errors and caching

- `TuleError` with structured fields (`op`, `faces`, `edges`, `hint`, data
  such as `max_radius`); OCCT exceptions never surface unwrapped.
- Operations cached by a hash of their inputs within a machine, so small
  edits rerun fast.

## Engineering libraries

- **`tule.dfm`** v0: CNC 3-axis (tool access and undercuts along a setup
  axis, internal corner radius vs tool, pocket depth ratio, thin walls) and
  FDM (overhangs, minimum wall, bridging, bed contact). Findings carry face
  ids, tags, severity and values (later shown as an `overlay` output).
- **`tule.topopt`** v0: SIMP on a hex grid (scipy sparse / scikit-fem);
  design domain, non-design regions, fixed faces and loads from tags; writes
  a `sequence` output as it iterates and a final smoothed isosurface `Shape`.
  A CUDA solver later, for GPU machines.
- **Later:** `tule.fea` (linear static, gmsh + scikit-fem), cost estimates,
  drawings.

## Repository layout

```
tule/
  python/tule/        modeling API + outputs, dfm, topopt (pip package)
  python/tests/
  cli/                the `tule` CLI (Python, pip package)
  runtime/            Dockerfiles (cpu, cuda), tule-agent, sshd config
  control/            Worker + Durable Objects: auth, MCP, backends, tunnel
  web/                the viewer and site (from genau web/src, minus Pyodide/wasm)
  templates/project/  new-project template with AGENTS.md, CLAUDE.md, tule.toml
  skills/             agent instructions (source for AGENTS.md, tule docs, plugin)
  examples/
```

## From genau

- Reused: `web/src` (viewer, projects, picker, parts panel, editor), the
  relay's Durable Object and WebSocket plumbing, the e2e test approach.
- Rewritten: the page's data source (outputs instead of Pyodide frames),
  viewer normals and wireframe inputs, examples, API docs.
- Dropped: `rust/`, `proofs/`, `tools/`, `bench/`, the wasm builds,
  coi-serviceworker, the relay's agent tools and stand-in, exactness docs.

## Milestones

1. **Spike: the round trip.** CPU runtime image with OCP and `tule-agent`; a
   Cloudflare Container per project that dials out; `tule ssh` through the
   tunnel; a STEP file written to `outputs/` appears in the copied viewer.
   Measure cold start and image size.
2. **API v0** (native, pytest): primitives, profiles, sweeps, Booleans,
   transforms, fillet/chamfer/shell, queries, provenance, tags, parameters,
   measurements, STEP (with names, colors, tags) and mesh export.
3. **Workspace:** R2 snapshots, sync, lock, restore; the project template.
4. **Accounts and connector:** Google sign-in, MCP server with OAuth, device
   codes for the CLI, API and SSH keys, quotas; skills, `tule docs`, golden
   paths, the Claude plugin.
5. **Viewer:** outputs (part, assembly, sequence), faces and edges, picking,
   files browser, snapshot history, STEP download.
6. **Engineering libraries:** `tule.dfm` v0, `tule.topopt` v0.
7. **GPU:** RunPod backend (settings, encrypted key, caps, `tule up --gpu`),
   agent-provisioned pods via join tokens, then `tule.remote`.
8. **Deploy** and a live smoke test.

## Open decisions (defaults assumed above)

| Decision | Default |
|---|---|
| Domain | `*.workers.dev` until one is chosen |
| Sign-in | Google, GitHub as an alternative |
| Default machine | `cpu-1` (1 vCPU / 6 GiB), up to `cpu-4` (4 vCPU / 12 GiB) |
| Outbound network | package registries + per-project allowlist |
| First GPU backend | RunPod |
| Python version | 3.12 (match the OCP wheels) |
| Workspace size limit | a few GB per project in v0 |
| Project visibility | private by default, shareable read-only viewer links |
