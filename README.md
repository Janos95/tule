# Tule

An engineering computer for your own AI agent: OpenCascade, a pythonic modeling API and engineering libraries in an isolated container per project, reached over SSH, with git as project state and a live web viewer.

See [DESIGN.md](DESIGN.md).

## Use it

Add `https://tule.janosmeny.com/mcp` to your agent as a custom connector
(Claude, ChatGPT) or remote MCP server (Claude Code, Codex, Cursor). That's it:
the agent installs and signs in the `tule` CLI itself (the connector's
`connect_info` gives it one command).

By hand:

```sh
curl -fsSL https://tule.janosmeny.com/cli.sh | sh    # installs the CLI; approve the sign-in in the browser
tule new bracket && tule up -p bracket
tule build -p bracket                 # runs the template's src/part.py on the machine
tule open -p bracket                  # the viewer shows outputs/part
tule save -p bracket -m "First bracket"   # commit and push the project's git history
```

A project can run on several machines at once, each with its own copy, merged
when saved. GPU machines run on your own RunPod account: paste a RunPod API key
in Settings, then `tule up --machine gpu-rtx4090` (`tule gpus` lists prices).

## Develop

```sh
pip install -e "python[test]" -e "cli[test]"   # the modeling API and the tule CLI
python -m pytest python cli           # tests

cd web && npm ci && npm run build     # the viewer, into web/dist
cd ../control && npm ci
npm run deploy                        # the Worker, with the machine image CI built for origin/main
```

CI (`.github/workflows/runtime.yml`) tests every push to main that changes the machine image, builds it and
pushes it to Cloudflare's registry as `tule-project-cpu:sha-<commit>` (needs the `CLOUDFLARE_API_TOKEN`
repository secret, with Containers: Edit). `npm run deploy` pins that image by digest, so it builds and
uploads nothing; push image changes first and wait for CI. `npm run deploy:local-image` builds and pushes
the image from this computer instead (needs Docker).

Secrets (`npx wrangler secret put …`): `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`ENCRYPTION_KEY` (32 random bytes, base64), `DEV_TOKEN` (a token for tests).

Local: `npx wrangler dev` in `control/` with a `.dev.vars` holding those plus
`DEV_LOGIN=1` (turns on `/auth/dev` and `tule admin pull`),
`PUBLIC_URL=http://127.0.0.1:8787` and `MACHINE_URL=http://host.docker.internal:8787`
(the container reaches the dev server through Docker's host name);
`tule login --url http://127.0.0.1:8787 --token <DEV_TOKEN>`.
`npm --prefix control run dev` also rebuilds the viewer on every change and reloads open pages.

A real project, to try changes on (nothing is deployed):

```sh
tule admin pull pomodore          # signed in to production: your project's newest autosave, into the dev server
docker build -f runtime/Dockerfile -t tule-runtime:dev .   # once, and when dependencies change
tools/dev-machine.sh pomodore     # a Docker machine running this checkout's python/, cli/ and runtime/
open http://127.0.0.1:8787/auth/dev?next=/p/pomodore
```

Someone else's project: `tule admin copy OWNER PROJECT` first, then pull the copy.
