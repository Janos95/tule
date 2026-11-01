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
4. **Connect RunPod (optional)** in Tule's settings: paste a RunPod API key;
   Tule validates it and shows the balance. RunPod's own limits (prepaid
   balance, account spend limit) are the budget; Tule adds no cap of its own.
   Later, users may simply buy compute from Tule instead of bringing a key.
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

- **Cloudflare** (`cpu-1`, `cpu-2`, `cpu-4`): the Project Durable Object is
  itself the container's Durable Object (the Durable Object Container API,
  `ctx.container`, with the `durable_object` scheduling policy, in beta), so
  each start picks the image and instance size and passes the join token in
  the environment: `cpu-1` = `standard-2` (1 vCPU, 6 GiB, 12 GB disk), `cpu-2`
  = `standard-3` (2 vCPU, 8 GiB), `cpu-4` = `standard-4` (4 vCPU, 12 GiB).
  The container keeps running while the Durable Object is active; the agent's
  heartbeat (every 30 s) keeps it active, and the Durable Object's own idle
  check (an alarm every 5 min) stops the machine after 30 min without SSH
  sessions or traffic. Paid by Tule's Cloudflare account; users get a CPU
  quota.
- **RunPod** (`gpu-<type>`): `POST` a pod to RunPod's REST API (v2,
  `api.runpod.io/v2`; v1 retires 2026-11-15) with the user's stored key,
  image `ghcr.io/janos95/tule-runtime:cuda`, the GPU type, no ports, and the
  join token in its environment; Secure Cloud first, then Community Cloud.
  Paid from the user's RunPod balance. Stopping takes the final snapshot and
  then terminates the pod (stopping a pod would keep its disk); the Project
  Durable Object checks the pod every minute and terminates one that died or
  never connected (20 min), so no pod bills unnoticed. Short names:
  `gpu-rtx4090`, `gpu-l40s`, `gpu-a100`, `gpu-h100`, … (`tule gpus` shows
  price and availability). The balance check uses RunPod's GraphQL API, the
  only place it exists (deprecated for 2027).
- Later backends (Modal, Lambda, a user's own box) implement the same four
  calls.

Every machine runs the same entrypoint, so nothing above the backend cares
where it runs:

1. `tule-agent` starts and restores `/workspace` from R2 through the control
   plane (chunk and snapshot endpoints that take the machine's join token;
   no storage credentials in the machine).
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
timeout (no SSH sessions, no running processes it was told about). Spending
limits come from RunPod: it stops pods when the prepaid balance reaches $0
and enforces an account spend limit.

**GPU functions from a CPU session** (later): `tule.remote(gpu="rtx4090",
max_cost=1.0)(fn)(...)` runs one function on a short-lived GPU machine with
the same image and a copy of the workspace, and writes its results back into
`/workspace`. Same backend interface; the CPU machine stays the owner of the
workspace.

**RunPod credentials:** the key is encrypted at rest in the Accounts Durable
Object (key from a Worker secret), used only by the control plane to create,
query and stop pods, and never passed into a machine. Creating pods needs a
key with write access to RunPod's API; the settings page says so and suggests
a dedicated key. Users who prefer not to store a key
can let their agent create the pod with their own RunPod connector, passing
a join token from Tule; the pod then joins the project the same way.

### The workspace

Like RunPod's `/workspace`: the only directory that persists. The rest of the
machine is rebuilt from the image every boot.

- **Everything under `/workspace` is the project:** code, uploaded data
  (supplier STEP files, load tables, material sheets, scans), notes, results,
  a virtualenv if the agent wants one.
- **Saved continuously** to R2 as content-addressed files: on change (3 s
  after the last change, at least every minute while it keeps changing), on
  stop (the Project Durable Object asks for a final snapshot before it stops
  the container; SIGTERM does the same), and on `tule snapshot`. Unchanged
  files are never uploaded twice, so snapshots are cheap; `tule snapshots`
  lists them and `tule restore <id>` goes back to one (saving the current
  state first). As built (milestone 3, `runtime/workspace.py`):
  - a snapshot is a manifest: per file its chunk hashes (sha256 of up to
    16 MiB), size, executable bit and mtime; symlinks by target; empty
    directories by name. Chunks live once per project at
    `projects/<p>/blobs/<sha256>`, manifests at
    `projects/<p>/snapshots/<id>.json`, the index (when, why, files, bytes)
    in the Project Durable Object;
  - the machine moves chunks through the Worker (it checks each chunk's
    hash), so it needs no storage credentials;
  - a machine is "running" only once its agent has restored `/workspace`
    (or written the template, for a new project): SSH never sees a half
    restored project;
  - ignored: `__pycache__`, `.pytest_cache` and the patterns in
    `.tuleignore`;
  - not yet: garbage collection of unreferenced chunks, a size limit.
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
  `"status": "running"` means more frames are coming), `overlay` (a part
  with faces colored by `role` or `severity` and labeled, plus arrows:
  boundary conditions, DFM findings, what the agent understood from an
  annotation; `{"file", "faces": {"12": {"role", "label"}}, "arrows":
  [{"at", "dir", "label"}]}`). Later: per-vertex data (stress) and
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
- Quotas: CPU machine-hours per user and month (`CPU_HOURS_MONTH`, 100 by
  default; Tule pays), counted by the Project Durable Object while a machine
  runs. GPU time is paid from the user's RunPod balance and limited by RunPod;
  Tule records hours and cost for the usage page. Not yet: a limit on
  concurrent machines.
- Sign-up is invite-only for now: `ALLOWED_EMAILS` lists who may sign in.

### The tunnel (as built in milestone 1)

One WebSocket from the machine carries everything (`runtime/tule_agent.py`
has the protocol). Text frames are JSON control messages (`hello`, `config`
with SSH keys and the host key, `outputs` with the index, `ssh-open`/
`ssh-close`, `mesh` requests, `beat`); binary frames are `[kind u8][id
u32][payload]`, kind 1 the bytes of SSH stream `id`, kind 2 a mesh for
request `id`.

- **SSH:** `tule proxy` opens `wss://<tule>/api/projects/<p>/ssh`; the Project
  Durable Object gives the stream an id and asks the agent to open it; the
  agent pipes it to sshd on `127.0.0.1:22` (bytes the client sends before
  sshd accepts are buffered). sshd allows only keys the project knows.
- **Host key:** the first machine of a project generates it and sends it to
  the Durable Object, which hands it to every later machine; `tule ssh` pins
  it in `~/.config/tule/known_hosts` (`StrictHostKeyChecking=yes`).
- **Keys:** the CLI makes its own key (`~/.config/tule/id_ed25519`) and adds
  it to the project the first time; `tule keys add` adds others.
- **Outputs:** the agent polls `outputs/` (0.5 s), hashes changed files and
  sends the index; the Durable Object stores the last index and broadcasts it
  to viewers. A viewer asks for a file by path and hash; the agent
  tessellates it (`import_step` + `tessellate`, cached by hash) and answers
  with positions, normals, indices, B-rep face ids, B-rep edge segments,
  name, color and tags (as face ids).
- **Viewer requests:** the page sends `{t: "req", rid, op, args}`; the
  Project Durable Object relays it to the agent unchanged (with its own
  request id) and the answer back, so a new request (annotations, the files
  browser) is a handler in `tule_agent.py` (`REQUESTS`) and code in the page,
  nothing in between. `{t: "broadcast"}` from the agent reaches every page.
- **Auth:** see "Accounts". Machines use their join token, which the
  Durable Object stores hashed and replaces on every start; they reach their
  project by its id (`/api/projects/p_…/agent`), users by its name.

### Accounts (as built in milestone 4)

- **One Accounts Durable Object** holds users (by Google subject), browser
  sessions, API keys, device codes, SSH keys, projects (name → id per user),
  the RunPod connection and usage. Secrets are stored as SHA-256 hashes; the
  RunPod key is encrypted (AES-GCM, key in the `ENCRYPTION_KEY` secret, the
  user id as associated data) and decrypted only for the control plane's own
  calls to RunPod.
- **Projects** are Project Durable Objects named by an id (`p_…`); users
  address them by name. SSH keys belong to the account and reach every
  project's machine (sent on connect and when they change).
- **Sign-in:** Google (OAuth code flow with PKCE) sets an HttpOnly session
  cookie for 30 days. `tule login` uses a device code: it shows a link and a
  code, the user approves it at `/device`, and the CLI receives an API key.
  API keys (`tule_…`) are also made in Settings. `DEV_TOKEN` remains as an
  admin credential acting as a "dev" user, for tests.
- **MCP:** `@cloudflare/workers-oauth-provider` wraps the Worker: `/mcp` takes
  its OAuth tokens (dynamic client registration, `/authorize` signs the user
  in with the site's session and asks for consent) or a Tule API key.
- **Pages** (server-rendered): the projects list, Settings (RunPod, usage, SSH
  and API keys), the device approval, the consent page.

## Interfaces

Modeled on RunPod's connector: the MCP manages resources, the work happens
over SSH, and skills route the agent between the two.

### MCP server (control plane)

Hosted at `/mcp`, Streamable HTTP. Auth: MCP OAuth (the user signs in with
Google and approves the agent), or a Tule API key as a Bearer header. Tools,
grouped by resource:

- **Projects:** list, create (from the template), rename, delete (fork later).
- **Machines:** start, stop, resize (`cpu-1`, `cpu-2`, `cpu-4`, `gpu-<type>`),
  status, the GPU list with prices; connection info for SSH; a join token for
  a machine the agent provisions itself (logs later).
- **Snapshots and outputs:** list snapshots, restore; list outputs with viewer
  links.
- **Keys:** SSH keys (list, add, remove), API keys (create, revoke).
- **Usage:** machine hours, GPU spend.

No exec or file tools: like RunPod's, the MCP never carries the work. Plus
`guide(topic)`, which serves `skills/*.md` (bundled at build time).

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

The agent instructions are written once and shipped through the MCP server
itself (server instructions plus a `guide(topic)` tool, so the bare connector
URL is self-sufficient in any app), in every project as `AGENTS.md`/`CLAUDE.md`,
and on the machine as `tule docs`. v0 ships only the connector URL; a Claude
plugin (the same URL plus auto-loading skills) and directory listings come
later. Content:

- a router (start here): when to use the MCP, when the CLI and SSH;
- setup: `tule login`, SSH key, connect, machines and GPUs;
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
  selector; STEP/3MF download; settings (RunPod key, API and SSH keys);
  usage.
- The code editor stays for small edits; saving writes to `/workspace`
  (not built yet).

As built (milestone 5): the outputs list; overlays
(faces colored by severity or role, load arrows, a legend); the sequence
plot; downloads; assembly placement (`"transform"`: a row-major 4×4 matrix per
part); CAD edges always drawn; and **annotations**: the
user draws strokes or picks and labels faces, and Save writes
`/workspace/annotations/<time>/annotation.png` and `annotation.json` (strokes
mapped to B-rep faces, labelled picks, the camera, a one-line `summary`) for
their agent to read, e.g. to place boundary conditions, which the agent shows
back as an overlay for the user to confirm before running topology
optimization. The page talks to the machine through the generic request relay
(`read`, `annotate`, `annotations`; the handlers are in `runtime/files.py`,
`runtime/annotations.py`). A files browser and snapshot history were built
and removed again: the agent works with files over SSH, and going back to a
snapshot is `tule restore` (or asking the agent). Settings and
usage are server pages (see "Accounts").

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
plate = plate.tag("mount", plate.faces["bottom"])
boss  = circle(9).extrude(12).at(0, 0, 6)
part  = (plate | boss).fillet(boss.edges["bottom"], 2)   # provenance survives the union
part -= cylinder(3.2, 50, center=True)
part  = part.shell(part.faces["mount"], 1.5)
outputs.part("bracket", part.named("bracket", "#4a8"))
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
  `.top()`, `s.edges.parallel(Z)`, `.on(faces)`, `.circles(r=)`,
  `.longest()`, `.within(lo, hi)`, `.nearest(p)`; `.ids` and `.info()` give
  stable ids and facts for agents. Seam edges are never selected.
- **Roles (provenance):** feature results name their faces
  (`boss.faces["side"]`, `box(...).faces["top"]`, `fillet`, `inner`, …);
  OCCT's builder history (`Modified`, `Generated`) carries them through later
  operations, so `part.fillet(boss.edges["bottom"], 2)` finds the boss's
  edge after a union. A sub-shape survives while it or one of its images is
  in the result; `IsDeleted` is not used (fillet and chamfer report every
  untouched edge as deleted). Roles do not carry through Booleans as names
  on the result (they would collide); the feature's own selection resolves.
- **Tags:** `plate.tag("load", plate.faces["top"])`, `part.faces["load"]`;
  they flow through every operation, are written into STEP files, and are
  what the analysis libraries read.
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
  ids, tags, severity and values, shown with `outputs.overlay(name, part,
  report)`. As built: `dfm.cnc(part, axis=-Z, tool_radius=3, ...)`,
  `dfm.fdm(part, up=+Z, max_overhang=45, ...)` return a `Report`; access and
  thickness come from ray casts on the tessellation (about the 0.05 mm
  tolerance); tool holders and shanks are not checked yet.
- **`tule.topopt`** v0: density-based SIMP (minimum compliance under a volume
  constraint) with a density filter and optimality-criteria updates (MMA can
  plug into the update table later). The solver decides how large a problem
  fits, so it is ours: matrix-free 8-node hexahedra on a voxel grid,
  conjugate gradients preconditioned by geometric multigrid (Galerkin coarse
  operators, damped Jacobi smoothing, a direct solve on the coarsest grid),
  and coarse-to-fine continuation. CG iterations stay flat as the grid gets
  finer (7–11 per solve from 2k to 147k elements in the benchmark). One code
  path for CPU and GPU through an array backend (NumPy/SciPy, CuPy on GPU
  machines; `device="auto"`). Tule integration: the domain is a `Shape`
  voxelized with OCC's classifier; `topopt.Problem(part, fixed=part.faces["bolt"],
  loads={part.faces["load"]: (0, 0, -500)}, keep=[...], void=[...])`, whose
  `.show(name)` writes the boundary conditions as an overlay to confirm;
  `optimize()` streams a `sequence` output and returns a smooth isosurface
  mesh (marching cubes behind one function, so dual contouring can replace
  it) that converts to a `Shape` for later Booleans. Manufacturing
  constraints (symmetry, extrusion and milling directions, overhang limits)
  come later with DFM. The GPU path is untested.
- **Later:** `tule.fea` (linear static, gmsh + scikit-fem), cost estimates,
  drawings.

## Repository layout

```
tule/
  python/tule/        modeling API + outputs, dfm, topopt (pip package)
  python/tests/
  cli/                the `tule` CLI (Python, pip package `tule-cli`)
  runtime/            Dockerfiles (cpu, later cuda), tule-agent
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
   Measure cold start and image size. **Done** (2026-11-01), measured on
   `cpu-1`:
   - image: 286 MB compressed, 1.2 GB on disk (VTK, which OCP links at
     import, pruned to OCP's library closure: 639 → 315 MB; Mesa's drivers
     removed: OCP only needs `libGL.so.1` to load);
   - cold start, `start()` to the agent connected: 11.0 s for the first start
     after a deploy (the host pulls the image), 1.1 s after that; `tule up`
     plus the first `tule ssh -- true`: 2.7 s;
   - a command over `tule ssh`: about 1.5 s; the bracket example over SSH:
     8.7 s (mostly importing OCP and tessellating);
   - the first mesh of the bracket for the viewer: 206 KB, 5,584 triangles.
   Not yet: `/workspace` survives nothing (milestone 3), the idle stop is
   untested, the viewer's token is in the WebSocket URL.
2. **API v0** (native, pytest): primitives, profiles, sweeps, Booleans,
   transforms, fillet/chamfer/shell, queries, provenance, tags, parameters,
   measurements, STEP (with names, colors, tags) and mesh export.
3. **Workspace:** R2 snapshots, sync, lock, restore; the project template.
   **Done** (2026-11-01): see "The workspace"; restoring a small project
   takes about 0.2 s.
4. **Accounts and connector:** Google sign-in, MCP server with OAuth, device
   codes for the CLI, API and SSH keys, quotas; skills, `tule docs`, golden
   paths, the Claude plugin. **Done** (2026-11-01) except the Claude plugin:
   see "Accounts".
5. **Viewer:** outputs (part, assembly, sequence), faces and edges, picking,
   STEP download. **Done** (2026-11-01), with overlays and annotations; the
   code editor is not built (the files browser and snapshot history were
   dropped, see "Viewer").
6. **Engineering libraries:** `tule.dfm` v0, `tule.topopt` v0. **Done**
   (2026-11-01): see "Engineering libraries".
7. **GPU:** RunPod backend (settings, encrypted key, `tule up --gpu`),
   agent-provisioned pods via join tokens, then `tule.remote`. The backend,
   settings and `tule up --gpu` are built; `tule.remote` is later.
8. **Deploy** and a live smoke test.

## Open decisions (defaults assumed above)

| Decision | Default |
|---|---|
| Domain | `tule.janosmeny.com` (a custom domain of the Worker; `tule.genau-mcp-relay.workers.dev` still answers) |
| Sign-in | Google, GitHub as an alternative |
| Default machine | `cpu-1` (1 vCPU / 6 GiB), also `cpu-2` (2 / 8) and `cpu-4` (4 vCPU / 12 GiB) |
| Outbound network | package registries + per-project allowlist |
| First GPU backend | RunPod |
| Python version | 3.10+ (tested on 3.13 with OCP 8.0) |
| Workspace size limit | a few GB per project in v0 |
| Project visibility | private by default, shareable read-only viewer links |
