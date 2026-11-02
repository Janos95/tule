# Tule — design

Tule is an engineering computer for your own AI agent. Each project gets an
isolated machine with OpenCascade, Tule's Python modeling API and engineering
libraries (topology optimization, later FEA) preinstalled. Your agent
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

1. **An engineering stack, ready and pinned.** OCCT, the `tule` API,
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

1. **Open the site:** it is the projects page, with nothing to sign up for:
   a first visit makes a guest account (see "Guests") to try Tule with. **Sign
   in** with Google (GitHub as an alternative) to keep projects.
2. **Add the connector** to the agent: in Claude or ChatGPT as a custom
   connector (`https://<tule>/mcp`), in Claude Code as a remote MCP server or
   the Tule plugin, in Codex/Cursor as a remote MCP server.
3. **Link the agent to the account.** The first time the agent uses the
   connector, the app opens Tule's sign-in page (MCP OAuth): the user signs
   in with Google and approves "Allow Claude to manage your Tule projects".
   That is the user's only step. The agent sets up its CLI itself:
   `connect_info` returns one command, `curl -fsSL <tule>/cli.sh | sh -s --
   tulel_…`, that installs the CLI (with uv, else pip) from Tule's own
   `/install/` wheel and signs it in with a code the connector approved in
   advance (works once, for an hour). The user never opens a terminal, copies
   a key or approves a code. Until the account's agent first uses the
   connector (MCP `initialize` sets `users.agent_seen`), the projects page
   shows a strip under the header: "Connect your agent", **Add to Claude**
   (Claude's own link that opens "Add custom connector" with Tule's name and
   address filled in; connectors live on the Claude account, so it also
   reaches the desktop and mobile apps and Claude Code), **Add to ChatGPT**
   (ChatGPT has no such link: it copies the address, opens chatgpt.com/plugins
   and says to click + → Add custom MCP server), and the address as a
   click-to-copy field. After that it is only in Settings.
4. **Connect RunPod (optional)** in Tule's settings: paste a RunPod API key;
   Tule validates it and shows the balance. RunPod's own limits (prepaid
   balance, account spend limit) are the budget; Tule adds no cap of its own.
   Later, users may simply buy compute from Tule instead of bringing a key.
5. **Create projects** on the site or by asking the agent ("start a Tule
   project for a camera bracket"); both call the same control plane.

Then the agent works: it loads the project on a machine, connects over SSH,
writes scripts in `src/`, builds `outputs/` with `tule build`, and the user
watches in the viewer.

## Architecture

```
  agent side                           Cloudflare (control plane)
 ┌───────────────────┐               ┌─────────────────────────────────────────┐
 │ agent             │──MCP (HTTPS)──▶ Worker: auth (Google OAuth, MCP OAuth,  │
 │ `tule` CLI / ssh  │──WSS tunnel───▶   device codes), MCP, CLI API,          │
 └───────────────────┘               │   viewer assets, tunnel endpoint        │
 ┌───────────────────┐               │   │                                     │
 │ browser: viewer,  │◀──WSS─────────│ Durable Object "Project" (one each)     │
 │ files, settings   │               │   - owns the project's machines         │
 └───────────────────┘               │   - routes SSH / viewer / sync streams  │
                                     │   - autosave index, git refs            │
                                     │   - usage and budget accounting         │
                                     │ Durable Object "Accounts": users,       │
                                     │   connections (RunPod key, encrypted),  │
                                     │   API keys, SSH keys, projects          │
                                     │ R2: snapshots, git bundles, LFS objects │
                                     └───────────────▲─────────────────────────┘
                                                     │ WSS, dialed OUT by the machine
                                  ┌──────────────────┴───────────────────┐
                                  │ machines (several per project)       │
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
- **Your own computers** (`<computer name>`): see below.
- Later backends (Modal, Lambda) implement the same four calls.

### Your own computers

A user can plug in their own computers (a Linux box with a GPU, a Mac mini)
as machines, modelled on T3 Code's remote machines (`t3 up`) but
containerized:

- **One command:** Settings → Machines → **Add a computer** shows
  `curl -fsSL <site>/install.sh | sh -s -- tulep_…`, with a one-time key
  (an hour, one computer) in it. Pasted into a terminal on the computer,
  `install.sh` (web/src/install.sh, served by the Worker) installs what is
  missing: Docker Engine on Linux (plus NVIDIA's container toolkit when
  `nvidia-smi` sees a GPU, and lingering so the service runs from boot),
  OrbStack on macOS (memory raised to all but 2 GB); then the tule CLI with
  uv (which brings its own Python; the wheel is served from `/install/`).
  Then it runs **`tule host up --key`**: the key buys the computer a
  host-only credential (`tuleh_…`, it can do nothing but serve as that
  computer), and a background service is installed: a systemd user unit on
  Linux, a LaunchAgent on macOS (while the user is logged in). Running the
  command again is safe (an already added computer stays as it is). Windows:
  inside WSL 2. Without a key, `tule host up` pairs by device code instead
  (approve in the browser).
- **Docker wherever it is:** the service finds the docker command on PATH or
  in OrbStack / Docker Desktop, their sockets when DOCKER_HOST is unset, and
  goes through `sg docker` right after Linux added the user to the group.
- **Connected by dialing out:** the service (`tule host run`) keeps one
  WebSocket to the computer's **Host** Durable Object and reports its specs
  (cores, memory, GPUs, architecture). No inbound ports, no tunnel software.
- **Machines on it:** `tule up -p bracket --on <computer>` (or
  `machine_start` with the computer's name): the Project Durable Object asks
  the Host object to start a container; the host runs Tule's machine image
  (`docker run`, the arch's image, `--gpus all` if it has GPUs, the join token
  in the environment, **no CPU or memory limits**: the container may use the
  whole computer). Inside, `tule-agent` dials in as on any machine, so SSH,
  the viewer and `/workspace` saving are the same as in the cloud. Stopping
  takes the final snapshot, then removes the container. Several projects may
  run on one computer at once; they share it.
- **The boundary is the image:** Tule can only ask a host to start or stop its
  own machine image (from `ghcr.io/janos95/tule-runtime`), never to run a
  command; containers are unprivileged and see none of the computer's files.
  This is the main difference from T3 Code, whose agents run as the user.
- **Accounts:** computers belong to a user, are listed in Settings (under "Machines":
  online state, specs, remove) and in `tule hosts`; no quota
  applies (the user's hardware). Guests cannot add computers.
- **Images:** `ghcr.io/janos95/tule-runtime:cpu` (amd64), `:cpu-arm64`
  (Apple Silicon and ARM Linux, natively) and `:cuda` (amd64) for computers
  with NVIDIA GPUs (when a computer has a GPU, its machines always get it).
  The host pulls before every start (a quick check when nothing changed) and
  uses the image it has if the registry is unreachable.

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

**Another machine** (`tule up --machine gpu-rtx4090`, the `machine_start`
tool) loads the project there too, with its own copy (see "Several machines
per project, saves in git"). Resizing the Tule cloud machine (`cpu-1/2/4`):
the Project Durable Object asks it to snapshot and stop, starts the new
size, and that copy resumes there.

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
key with write access to RunPod's API; the settings page says so. Users who prefer not to store a key
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
- **A copy per machine:** each machine a project is loaded on autosaves its
  own copy (the snapshots above, tagged with the machine); no lock.
- **Git inside:** `/workspace` is a git repository whose remote is hosted by
  Tule; a save is a commit pushed to it (see "Several machines per project,
  saves in git"). Snapshots catch everything git does not, so nothing is lost
  even if the agent never saves.
- **`tule.toml`:** extra packages to install at boot, pip and apt (a slicer;
  the heavy stack is in the image).
- **New projects** start from a template: `AGENTS.md`/`CLAUDE.md` with the
  agent instructions, `tule.toml`, `src/part.py`, empty `outputs/`.

#### Several machines per project, saves in git (2026-11-01)

A project can be loaded on several machines at once, e.g. the agent
iterating on designs on Tule cloud while an FEA run goes on a RunPod GPU.
There are two separate kinds of saving:

- **A project is a history; a machine holds a working copy.** The Project
  Durable Object keeps a slot per machine: `cloud` (the one Tule cloud
  machine; cpu-1/2/4 resize it), `gpu-<type>`, a computer's name, and
  `joined` (a join token). Each slot has its own join token, autosave
  pointer, outputs, billing, idle stop and unsaved count. A project with no
  running slot is loaded nowhere.
- **Autosave is crash safety, not history.** The snapshots are per copy.
  They take the whole `/workspace`, unsaved and unmerged work included, so
  nothing is lost when a machine disconnects or a pod is preempted. They
  never commit or merge.
- **Save is git.** `tule save` (the `save` MCP tool, or on the machine) adds
  everything, commits, fetches, merges main, and pushes to main. Work on
  different parts merges cleanly; a conflict stops the save, lists the
  files, and the agent resolves it with ordinary git and saves again.
- **The remote** is a git remote helper (`git-remote-tule`, installed with
  the CLI, like git-remote-s3): the refs live in the Project Durable Object,
  each push adds one git bundle in R2
  (`projects/<id>/git/<n>.bundle`) holding the commits the remote lacked, and
  a fetch applies the missing bundles in order. A push is compare-and-swap
  on the ref, so a non-fast-forward push is refused. Chosen over git's smart
  HTTP protocol, which would need pack negotiation in the Worker. Auth: the
  machine's join token or the user's API key.
- **Large files via Git LFS.** Tule serves the LFS batch API (basic
  transfer) from R2 (`projects/<id>/lfs/<oid>`, checked against its
  SHA-256 on upload), so big files are never merged and never stored twice.
  Enforced, not left to the agent:
  1. the project template's `.gitattributes` sends the usual binary types to
     LFS (STEP, STL, 3MF, meshes, simulation results, images, archives);
  2. a pre-commit hook in the image moves any other file over 5 MB to LFS
     and adds its pattern to `.gitattributes`;
  3. the remote helper refuses a push with a plain blob over 5 MB, and the
     error gives the `git lfs migrate import` command that fixes it (for a
     commit made with the hook off or elsewhere).
- **Which state a machine gets:** a machine that comes back resumes its own
  last autosave; a machine newly loaded with the project starts from the
  latest save (main); a new project's template is committed and pushed as
  main. Projects from before git saves start from their newest autosave.
- **Unsaved work stays visible:** a stopped machine with unsaved work stays
  on the project ("stopped · unsaved") until it is loaded again and saved,
  or forgotten (`tule forget --machine …`, `machine_forget`).
- **Choosing a machine:** commands take `--machine` (MCP: `machine`), and
  need it only when several run. SSH host names are `tule-<project>.<slot>`.
- **UI:** the projects list shows each project with its machines as rows
  under it: an icon for where it runs (Tule cloud, RunPod, or the computer's
  OS), its name and GPU if any, and its state ("running", "starting",
  "· unsaved"); a stopped, saved copy is not shown. The viewer shows one copy;
  its footer picks which when several are loaded, and offers Save while the
  copy has unsaved work (what `tule save` does; a conflict is left to the
  agent). There is no view of main
  alone: outputs come from a running copy.
- **Why per project** (decided 2026-11-01): machines per user with projects
  mounted onto them were considered (instant switching, cross-project work,
  one GPU pod). Per project keeps parallel agents apart, gives each project
  a clean environment (`tule.toml`) and simple sharing, and a cached cold
  start is ~1 s anyway. Cross-project reuse would come as read-only mounts
  of another project's save.
- **Hosting:** Tule hosts the repositories so one connector is still all a
  user sets up. Pushing to (or mirroring on) GitHub can come later.
- **Open:** a merged `tule.toml` (packages) may be a combination neither
  machine ran, so a save that touches it should rebuild before results are
  trusted; outputs could record the commit they were made from ("from abc,
  2 saves behind"); garbage collection of git bundles (repacking).

### Outputs

The viewer renders `/workspace/outputs/` (and `work/`, `inputs/`): one folder per
output, holding geometry files, an optional `output.json`, and any other
files, each with its own view (see "Project layout, builds and output
types").

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
  with faces colored by `role` or `severity` and labeled, or by a number
  `value` on a color scale, plus arrows: boundary conditions, per-face checks
  and measurements, what the agent understood from an annotation;
  `{"file", "faces": {"12": {"role" | "severity" | "value", "label"}},
  "scale": {"label", "unit", "min", "max"}, "arrows": [{"at", "dir",
  "label"}]}`). Any kind may carry `"info"`: facts about the output
  (material, mass, ...), shown as rows under the view. The viewer shows
  info, the value scale and an overlay's legend in a strip under the view,
  never over the part. Later: per-vertex data (stress) and `variants` (side
  by side).
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
  show any output as of any snapshot. They are not in git (they are
  generated; see "Project layout, builds and output types").
- `tule.outputs` has small helpers that write these folders; nothing requires
  them.

#### Project layout, builds and output types (2026-11-02)

Built (2026-11-02): the layout, `tule build`, the template (`src/part.py`),
the output types below, failed builds in the viewer, and `tule based-on`.
Not yet: 3MF/OBJ/glTF in 3D (listed as downloads for now).
Outputs can be out of step with the code that makes them (edit a script and
forget to run it; a save merges code nobody ran), and nothing said so. Hence
a fixed layout, one build command that knows what made every output, and a
view for every kind of output.

**Layout.** Files are sorted by role, not by type:

```
inputs/    from outside the project: supplier STEP, scans, datasheets,
           catalogs, customer load cases (read-only to scripts; big files
           in LFS); never out of date
src/       scripts; src/<name>.py makes outputs/<name>/ (a part, an
           assembly, a BOM, an FEA run: all alike); one level of folders
           groups them (src/checks/fit.py makes outputs/checks/fit/)
lib/       shared code the scripts import; makes no outputs
work/      done by hand (the agent or the user) from the design: supplier
           quotes, part comparisons, reviews; in git, read by scripts,
           never re-run, flagged to check again when its sources change
outputs/   made by scripts only, never edited by hand, not in git
```

- Choices are code, not data: a wall thickness, a safety factor, which load
  case to run live in the scripts (or `lib/params.py`). `inputs/` holds only
  facts nobody here chose.
- The template starts with `src/part.py` (it was `main.py`); the other
  folders appear when needed.
- **Groups** (2026-11-02): QA runs with 10 to 30 outputs (revisions,
  alternatives, fit checks) had no way to tidy the list, and agents deleted
  scripts or nested outputs the viewer could not show. A folder in `src/` is
  a group: its outputs sit in `outputs/<group>/`, and the viewer shows the
  group as one folded row (open when it holds the selected output). One level
  only; the output inside may be called by its short name (`outputs.part("fit", …)`).

**Scripts are functions.** A script reads inputs, code and other outputs, and
writes only its own output folder. An assembly is a script that writes
several files. No registry: the file name is the target, and extra facts
(title, units) go in the script or `output.json`.

**`tule build`** runs the scripts whose fingerprint changed and skips the
rest:

- the fingerprint covers what the script actually read, recorded while it
  runs (a Python audit hook: file opens, folder listings, imports; files
  read by native code, like `import_step`, are reported by `tule`): the
  script, the local modules it imports, the files it reads in `inputs/`, `work/` and
  `outputs/`, and the versions of `tule` and Python. Nothing is declared, so
  nothing can be forgotten. Files are compared by content, so a change that
  does not change an output (a comment) rebuilds nothing after it; STEP
  files have no time of writing in them for this reason;
- a script that reads `outputs/bracket/` gets `bracket` built first, so the
  order needs no declaring either;
- each script writes into a fresh staging folder (`outputs/.build/`) that
  replaces its output only when it succeeds; writing anywhere else in the
  project is refused. While it runs, the viewer shows the staging folder once
  it has geometry, so a long study's frames still appear live;
- the record (`outputs/.build/<name>.json`: what was read and its hashes) is
  kept apart from the output, so the agent can tell an output is stale
  without running anything: `tule build --check` lists what is out of date
  or failed (with the reason and how long its last run took), outputs whose
  script is gone, and work to check again, or says everything is up to date;
  a project is the folder with `tule.toml` and `src/` (or a machine's
  /workspace), since any repository may have a `src/`;
- a failed script keeps its last output and records the error and the end
  of its output (the traceback from the script's own frames); one build
  runs at a time per project;
- an output that reads an out-of-date output is out of date too ("outputs/
  plate is out of date"), so the check shows everything a change affects;
- subsets: `tule build plate` (and what it reads), patterns (`'plate-*'`),
  `--dependents` (also what reads them: the BOM after the plate), `--skip
  study` (leave a long run as it is).

**Outputs travel through a build cache** (decided 2026-11-02). Outputs are
not in git, so a study built on one machine (a GPU pod, one of the user's
computers) used to stay on that machine's copy. Now every output a machine
builds is uploaded with its record: the files as chunks in the project's
blob store (shared with snapshots, so only new bytes move) and the record
under `projects/<id>/cache/<script>/` (the newest 5 per script). When an
output is out of date on another machine, `tule build` looks for an upload
whose record matches the files there (same script, modules, inputs, outputs
read, and tule and Python versions) and downloads it instead of running the
script; `--check` says so ("built on gpu-rtx4090: tule build downloads
it"); `--force` runs it anyway. Every output is uploaded, not only slow
ones: a slow output records the bytes of the outputs it read, so those must
be the same bytes on the other machine. Considered and not done: outputs in
git (every rerun of a big study in history, and merges of files nobody
edits), and showing a stopped machine's outputs (it would have to start).

**The agent keeps outputs current; nothing rebuilds by itself** (decided
2026-11-02). Rebuilding on every change was considered (after a pause in
edits, only for quick scripts): it would keep the viewer always current,
but it runs half-finished edits, spends machine time and GPU money on runs
nobody asked for, and is hard to predict. The agent builds what it changed
before it shows the user anything and leaves long runs until they are
needed. The viewer does not show staleness either (an earlier version had
"out of date" marks with Rebuild and Still valid buttons): it says only when
a build failed; a row's tooltip says when it is out of date.

**Work done by hand is not re-run.** Finding suppliers, comparing bearings or
reviewing a design is slow, costs money and takes judgment. Its result is
saved in `work/` (e.g. `work/suppliers/bracket.json`), and scripts read it
(`src/bom.py` puts prices in the BOM). It is not an input (it is made from
the design, so it can go stale; inputs come from outside and cannot) and not
an output (no script can remake it); a first version kept it in `inputs/`,
which mixed the two. `tule based-on work/suppliers/bracket.json
outputs/bracket` records what it was based on (`work/.based-on.json`, in
git: each source's fingerprint, a hash over a folder's files), so when the
bracket changes `tule build` asks to check it again. The agent then redoes
the work or confirms it still holds (`tule based-on <result>` again);
confirming updates only the note. Nothing ever re-runs an agent by
itself.

**Output types.** Every output gets a structured view; the file type decides
which, and `output.json` only adds meaning (titles, units, colors):

| Type | Files | View |
|---|---|---|
| geometry | STEP, STL, 3MF, OBJ, glTF | 3D: a part, or an assembly when there are several files |
| sequence | numbered geometry files | 3D with a slider and a plot of the values |
| overlay | geometry plus per-face values | 3D colored by value, with a legend |
| table | CSV, TSV, a JSON list of records | a sortable table (BOM, results, supplier lists) |
| chart | `*.vl.json` (Vega-Lite) | the chart, drawn by Tule |
| document | Markdown, PDF | the rendered page (reports, datasheets, notes) |
| image | PNG, JPG, SVG | the image |
| text | TXT, logs, code, TOML, YAML | the text |
| anything else | any file | listed with a download, never hidden |

**In the viewer** (built): the sidebar lists outputs, then work, then inputs,
one row per folder; a row says "failed" when its last build failed, and its
tooltip says when it is out of date ("out of date: lib/params.py changed")
or work needs checking again. Under the selected entry: its views
when it has more than one: "3D" for its geometry (an assembly's parts under
it), each other file, and a failed build's log. A view that is not 3D is a
page over the stage. The machine's agent sends every file in the index with
the build state of each output and the "based on" state of each piece of work
(`runtime/tule_agent.py`, `Outputs`). Files are read through the agent (cached by
content hash); Vega-Lite and the Markdown renderer load only when a chart or
document is shown. JSON lists of records are tables; Markdown documents
embed charts, tables and images next to them (`![](cost.vl.json)`).

- **Charts are Vega-Lite**, not HTML: data plus a description of how to show
  it. No agent code runs in the page; Tule renders it with its own colors,
  dark mode and phone layout; agents already know the format. Data comes
  only from files in the same output folder (`"data": {"url":
  "results.csv"}`), never the network. Arbitrary HTML was considered and
  left out: it needs a separate origin and a network sandbox to be safe, and
  it cannot link to the rest of the viewer.
- **Links between views:** a table column or chart field that names an
  output (`part: bracket-v3`) links to it, so clicking a BOM row or a point
  on a Pareto chart selects that part in 3D.
- **Inputs and work use the same views:** a supplier STEP opens in 3D, a
  datasheet PDF as a page, a JSON list of supplier quotes as a table.
- **Drawings** (built 2026-11-02): `outputs.drawing(name, part)` writes a
  dimensioned 2D drawing as `<name>.pdf` and `<name>.svg` (a document and an
  image, so no new view), and with `dxf="top"` that view at 1:1 as an R12
  DXF for laser, waterjet or CNC (a download). Agents used to draw these by
  hand with reportlab, computing projections and dimension positions
  themselves; the drawings went stale and had stray leaders. Now they come
  from the model (`tule/drawing.py`): views by OCCT hidden-line removal
  (hidden edges dashed; a mesh-based algorithm for very large parts), placed
  by ISO first-angle (or third-angle) projection with an isometric view in a
  free spot; overall sizes, each once, and callouts found from the faces in
  standard form: holes (`4× ⌀6.6 THRU`, `↧` depth for blind ones) with their
  counterbore (`⌴ ⌀11 ↧6.5`) or countersink (`⌵ ⌀12 × 90°`), slots (two
  facing half-cylinders: `4× slot 5.5 × 11.5 THRU`, width × overall length);
  center lines on holes, bosses and slots; dimensions only the author knows
  are given as 3D points or circular edges (`dims=[("top", p1, p2)]`).
  Tolerances go on any size by its printed text (`tol={"⌀22": "+0.05/0"}`,
  deviations stacked, fits like `H7`; a key that matches nothing is an error
  listing the sizes drawn) or per dimension. The title block has part
  number, revision, material, finish, general tolerances (ISO 2768-m unless
  told), drawn/date only when given; a revision table and a NOTES block go
  above it (or notes bottom left when that gives a larger scale). Every text
  is placed after all lines, on the first spot that touches no line and no
  other text (dimension text slides along its line or moves past its end;
  callouts move along their column), and `drawing.collisions(page)` checks a
  finished sheet; tests hold dense parts (a NEMA 23 plate) to zero. Added
  2026-11-02 after a QA agent read `help(outputs.drawing)` and still built
  its motor plate drawings with reportlab, for tolerances, counterbore/slot
  callouts, notes and revision. Symbols Helvetica lacks (⌴ ⌵ ↧) are drawn as
  strokes. PDF, SVG and DXF are written by hand (no new dependencies) and
  have no dates, so an unchanged part gives the same bytes. Not done: section
  views, detail views, surface finish and GD&T symbols, hole tables, hole
  patterns (bolt circles) found automatically, threads (an agent writes
  `M5` as callout text).
- Later: per-vertex results (stress), variants side by side.

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
- **Outputs:** the agent polls `outputs/`, `work/`, `inputs/`, `src/` and `lib/`
  (0.5 s; the last two only for build state), hashes changed files and sends
  the index; the Durable Object stores the last index and broadcasts it
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
  `tule login --code tulel_…` redeems a device code the connector's
  `connect_info` approved in advance, so agents sign in without the user.
  API keys (`tule_…`) are also made in Settings. `DEV_TOKEN` remains as an
  admin credential acting as a "dev" user, for tests, and is the only key to
  `GET /api/admin/users` (every account, with project and computer counts) and
  `POST /api/admin/users/delete {"email"}` (an account with its projects,
  snapshots, computers, keys and connector sign-ins).
- **MCP:** `@cloudflare/workers-oauth-provider` wraps the Worker: `/mcp` takes
  its OAuth tokens (dynamic client registration, `/authorize` signs the user
  in with the site's session and asks for consent) or a Tule API key.
- **Pages** (server-rendered): the projects list (the site's front page),
  Settings (four short sections: the connector; machines, meaning Tule
  cloud with this month's hours, your computers and RunPod; SSH and API keys
  in one list; account; no explanations beyond one note per add row),
  the device approval, the consent page.

### Guests

- A first visit to `/` without a session makes a **guest** user (no Google
  identity) with a session cookie, so Tule can be tried without signing in.
  Everything is stored under the guest's user id exactly as for an account;
  the cookie is the only way back to it (and any CLI the guest logged in).
- **Limits** (vars, the stricter defaults): `GUEST_PROJECTS` 1 project,
  `GUEST_MINUTES_DAY` 30 machine minutes a day (checked at start and every
  minute while running), `cpu-1` only with a 10-minute idle stop, no GPUs,
  `GUEST_MACHINES` 2 guest machines running at once site-wide, at most 5 new
  guests per IP address a day.
- **Expiry:** each visit updates `last_seen` (at most hourly); an hourly sweep
  in the Accounts object deletes guests not seen for `GUEST_DAYS` (1) with
  their projects (machines stopped, snapshots deleted).
- **Signing in** from a guest session moves the guest's projects (renamed on a
  name clash), keys and usage to the account; snapshots stay where they are,
  since they are stored by project id. Signing in itself stays invite-only
  (`ALLOWED_EMAILS`).

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
tule login [--code tulel_…]    # device code: approve in the browser, or a code from connect_info
tule keys add ~/.ssh/id_ed25519.pub
tule new bracket               # create a project from the template
tule up [--machine gpu-rtx4090] # load the project on a machine (others keep running)
tule save [-m "what changed"]  # commit, merge the latest save, push it
tule ssh [cmd]                 # shell or command on the machine (also: plain ssh via ProxyCommand)
tule build [names] [--check]   # run the scripts in src/ whose inputs changed; --check: what needs it
tule sync [--pull]             # copy /workspace to or from a local folder
tule screenshot -o /tmp/view.png [--output bracket] [--labels] [--view iso]
tule open                      # open the viewer for this project
tule snapshot | down | forget | ls | logs   # --machine picks one when several run
```

`tule ssh --config` writes an `~/.ssh/config` entry with
`ProxyCommand tule proxy %h`, so plain `ssh tule-bracket` works. Agents in
chat sandboxes can use the same CLI when the Tule domain is on their network
allowlist (the tunnel is HTTPS), or when the chat is linked to the user's
computer; otherwise a chat agent can manage projects through the MCP but not
work inside them (the same limit as RunPod).

### On the machine

- `tule build` (or any script, any tool); results go to `outputs/`.
- `tule screenshot`, `tule docs [topic]`, `tule inspect <file>`.
- The libraries: `tule` (modeling), `tule.topopt`, `tule.outputs`,
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
- the loop: edit a script → `tule build` → `tule screenshot` → inspect →
  iterate; the project layout, work done by hand, variants;
- topology optimization: loads, fixed faces;
- OCCT pitfalls (fillets, tolerances, sewing) and how errors read;
- golden paths: tested end-to-end examples ("bracket from load cases",
  "run a study on a GPU").

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

### Colors

One palette, "Reed", in `web/src/palette.css`; nothing else defines a color.
From the logo: **navy** ink (`#23344a`) for text, **reed** gold (`#c98a3a`,
lighter `#e0a45a`) for actions, selection and focus (buttons are reed with
dark text; links a darker reed), **stem** green (`#4f8a3c`, lighter
`#8fc46a`) for running, on warm paper surfaces. Status colors come in text,
fill and background shades (ok, warn, danger, info). The pages import the file
as text (wrangler `rules`), the viewer's stylesheet starts with it, and the
viewer's overlay colors read the same variables at runtime. Dark mode flips
the same names (pages only for now; the viewer opts out). The logo uses navy,
reed-light and stem-light on its tile; reed and stem without it
(`tools/logo.py`).

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
- Gears (2026-11-02): `spur_gear`, `internal_gear`, `gear_pair`. In a QA
  run an agent sampled involutes into polygons by hand: a 20-tooth pinion
  had 1263 faces, each gear was 20–24 MB of STEP, meshing stalled and every
  gearing check was its own code. Now the teeth are what a DIN 867 rack
  cuts: one B-spline per involute flank (within 0.0001 module), the rack
  tip's trochoid as root fillet (undercut where a real gear is), four faces
  per tooth (`flanks`, `tips`, `roots`), under 1 MB for 40 teeth.
  `gear_pair` gives the center distance (with profile shift), the contact
  ratio and `place` (gear 2 turned to mesh), and raises when the pair
  interferes (checked by turning it through a pitch) or the contact ratio
  is under 1.2, with a profile shift that works as the hint. Not built:
  helical and bevel gears, racks.
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
- **Parameters** are plain variables at the top of a script, or in
  `lib/params.py` when scripts share them: choices are code (see "Project
  layout, builds and output types"). A variant is a script that calls a
  shared function with other values.

### Errors and caching

- `TuleError` with structured fields (`op`, `faces`, `edges`, `hint`, data
  such as `max_radius`); OCCT exceptions never surface unwrapped.
- Operations cached by a hash of their inputs within a machine, so small
  edits rerun fast.

## Engineering libraries

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
  it) that converts to a `Shape` for later Booleans. The GPU path
  is untested.
- **Not now:** design-for-manufacturing checks (CNC, 3D printing). A `tule.dfm`
  v0 was built and removed on 2026-11-01 to keep the feature set small;
  overlays with a `severity` per face still show checks an agent writes itself.
- **Later:** `tule.fea` (linear static, gmsh + scikit-fem), cost estimates.

## Repository layout

```
tule/
  python/tule/        modeling API + outputs, topopt (pip package)
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
6. **Engineering libraries:** `tule.topopt` v0. **Done** (2026-11-01): see
   "Engineering libraries". `tule.dfm` was built and then removed
   (2026-11-01): a small feature set done well comes first.
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
