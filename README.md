# Tule

Give your AI agent an engineering computer. Each project gets an isolated machine with OpenCascade, a pythonic
modeling API and engineering libraries (FEA, topology optimization). The agent reaches it over SSH, git holds the
project's state, and a web viewer shows the parts in 3D as they change.

How it fits together: [DESIGN.md](DESIGN.md).

## Use it

Add `https://tule.janosmeny.com/mcp` to your agent as a custom connector (Claude, ChatGPT) or as a remote MCP
server (Claude Code, Codex, Cursor). The agent then installs and signs in the `tule` CLI itself: the connector's
`connect_info` gives it the command.

By hand:

```sh
curl -fsSL https://tule.janosmeny.com/cli.sh | sh   # install the CLI, then approve the sign-in in the browser
tule new bracket && tule up -p bracket
tule build -p bracket                    # runs the template's src/part.py on the machine
tule open -p bracket                     # the viewer shows outputs/part
tule save -p bracket -m "First bracket"  # commit and push the project's history
```

Projects run on Tule cloud by default. They can also run on your own computers (Connect a computer on the
projects page) or on GPUs in your RunPod account (a RunPod API key in Settings, then
`tule up --machine gpu-rtx4090`; `tule gpus` lists prices). A project can run on several machines at once, each
with its own copy, merged when saved.

## Repository

| Folder | What's in it |
|---|---|
| `python/` | the `tule` modeling library: shapes, assemblies, FEA, topology optimization |
| `cli/` | the `tule` CLI |
| `control/` | the control plane: a Cloudflare Worker with Durable Objects and Containers |
| `web/` | the 3D viewer |
| `runtime/` | the machine images (`Dockerfile` CPU, `Dockerfile.cuda` GPU) |
| `skills/`, `templates/` | what agents read, and the files a new project starts with |
| `examples/` | example parts |
| `qa/` | real coding agents use Tule with a simulated user; a reviewer reports what went wrong |
| `tools/` | dev scripts (`dev-machine.sh`, `build_agents_md.py`, benchmarks) |

## Develop

```sh
pip install -e "python[test]" -e "cli[test]"   # the library and the CLI
python -m pytest python cli                    # tests
cd web && npm ci && cd ../control && npm ci    # the viewer and the Worker
```

### Run it locally

In `control/`, create a `.dev.vars` with the secrets below, plus `DEV_LOGIN=1` (turns on `/auth/dev` and
`tule admin pull`), `PUBLIC_URL=http://127.0.0.1:8787` and `MACHINE_URL=http://host.docker.internal:8787` (the
container reaches the dev server through Docker's host name). Then:

```sh
npm --prefix control run dev     # the Worker on 127.0.0.1:8787; rebuilds the viewer and reloads open pages
tule login --url http://127.0.0.1:8787 --token <DEV_TOKEN>
```

To try changes on a real project (nothing is deployed):

```sh
tule admin pull pomodore          # signed in to production: the project's newest autosave, into the dev server
docker build -f runtime/Dockerfile -t tule-runtime:dev .   # once, and when dependencies change
tools/dev-machine.sh pomodore     # a Docker machine running this checkout's python/, cli/ and runtime/
open http://127.0.0.1:8787/auth/dev?next=/p/pomodore
```

For someone else's project, run `tule admin copy OWNER PROJECT` first, then pull the copy.

### Deploy

```sh
cd control && npm run deploy     # the viewer and the Worker, with the machine image CI built
```

CI (`.github/workflows/runtime.yml`) runs the tests on every push to main that changes the machine image. It then
builds the images, pushes them to GHCR, and pushes the CPU image to Cloudflare's registry as
`tule-project-cpu:sha-<commit>`. `npm run deploy` deploys that image, pinned by digest, so nothing is built or
uploaded from your computer. Push image changes first and wait for CI. `npm run deploy:local-image` builds and
pushes the image from your computer instead (needs Docker).

Worker secrets (`npx wrangler secret put …`): `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `ENCRYPTION_KEY`
(32 random bytes, base64), `DEV_TOKEN` (a token for tests). The CI push needs the repository secret
`CLOUDFLARE_API_TOKEN`, a Cloudflare API token with Containers: Edit.
