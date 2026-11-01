# Tule

An engineering computer for your own AI agent: OpenCascade, a pythonic modeling API and engineering libraries in an isolated container per project, reached over SSH, with git as project state and a live web viewer.

See [DESIGN.md](DESIGN.md).

## Use it

1. Sign in at https://tule.janosmeny.com (Google; invite-only for now).
2. Connect your agent: add `https://tule.janosmeny.com/mcp` as a custom
   connector (Claude, ChatGPT) or remote MCP server (Claude Code, Codex, Cursor).
3. Give it the CLI for the work:

```sh
pip install "tule-cli @ git+https://github.com/Janos95/tule#subdirectory=cli"
tule login                            # approve the code in the browser once
tule new bracket && tule up -p bracket
tule ssh -p bracket -- python - < examples/bracket.py
tule open -p bracket                  # the viewer shows outputs/bracket
```

GPU machines run on your own RunPod account: paste a RunPod API key in Settings,
then `tule up --gpu rtx4090` (`tule gpus` lists prices).

## Develop

```sh
pip install -e "python[test]" -e "cli[test]"   # the modeling API and the tule CLI
python -m pytest python cli           # tests

cd web && npm ci && npm run build     # the viewer, into web/dist
cd ../control && npm ci
npx wrangler deploy                   # Worker, Durable Objects, the machine image (needs Docker)
```

Secrets (`npx wrangler secret put …`): `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`ENCRYPTION_KEY` (32 random bytes, base64), `DEV_TOKEN` (an admin token for tests).

Local: `npx wrangler dev` in `control/` with a `.dev.vars` holding those plus
`PUBLIC_URL=http://127.0.0.1:8787` and `MACHINE_URL=http://host.docker.internal:8787`
(the container reaches the dev server through Docker's host name);
`tule login --url http://127.0.0.1:8787 --token <DEV_TOKEN>`.
