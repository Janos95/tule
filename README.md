# Tule

An engineering computer for your own AI agent: OpenCascade, a pythonic modeling API and engineering libraries in an isolated container per project, reached over SSH, with git as project state and a live web viewer.

See [DESIGN.md](DESIGN.md).

## Run it

```sh
pip install -e python -e cli          # the modeling API and the tule CLI
python -m pytest python               # API tests

cd web && npm ci && npm run build     # the viewer, into web/dist
cd ../control && npm ci
npx wrangler deploy                   # Worker + Project Durable Object + the machine image (needs Docker)
openssl rand -base64 32 | npx wrangler secret put DEV_TOKEN

tule login --url https://tule.<subdomain>.workers.dev --token <DEV_TOKEN>
tule up -p bracket                    # start the project's machine
tule ssh -p bracket -- python - < examples/bracket.py
tule open -p bracket                  # the viewer shows outputs/bracket
```

Local: `npx wrangler dev` in `control/` with a `.dev.vars` holding
`DEV_TOKEN=…` and `MACHINE_URL=http://host.docker.internal:8787` (the
container reaches the dev server through Docker's host name).
