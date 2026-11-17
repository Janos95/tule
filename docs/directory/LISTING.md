# Directory listings: Claude and ChatGPT

What to paste into the two submission portals. Server URL: `<origin>/mcp`
(today `https://tule.computer/mcp`). Changing the origin later means a new
ChatGPT submission, so submit on the domain Tule will keep.

## Shared facts

- **Auth:** OAuth 2.1 with PKCE (S256) and dynamic client registration
  (`/.well-known/oauth-authorization-server`, `/oauth/register`). Any redirect
  URI is accepted at registration, including `https://claude.ai/api/mcp/auth_callback`.
- **Reviewer access:** no credentials needed. On the connect page choose **Try
  without an account**, then **Allow**. That makes a guest account with full
  functionality, kept for a day after its last use (limits: 1 project, 30 machine
  minutes a day, CPU machines only). If a form insists on a login: any Google
  account works, and sign-up is open.
- **Privacy policy:** `<origin>/privacy` · **Terms:** `<origin>/terms`
- **Website / docs:** `<origin>` and `https://github.com/Janos95/tule` (README)
- **Support:** CONTACT_EMAIL (set it in `control/wrangler.jsonc`), else GitHub issues
- **Icon:** `tule-icon-512.png` (512×512 PNG, square)
- **Tools (5), all hints explicit:**

  | tool | title | readOnly | destructive | idempotent | openWorld |
  |---|---|---|---|---|---|
  | guide | Read Tule's guide | true | false | true | false |
  | info | Read projects, machines and account | true | false | true | false |
  | project | Create, save or delete a project | false | true | false | false |
  | machine | Start, stop or restore a machine | false | true | false | false |
  | keys | Add or revoke keys | false | true | false | false |


## Name and descriptions

- **Name:** Tule
- **One-liner (Claude, ≤200):** An engineering computer for your AI agent: CAD
  with OpenCascade, simulation and 3D-print plates on cloud machines, with a live
  3D viewer.
- **Short description (ChatGPT, ≤30):** CAD and engineering for agents
- **Long description (≤2000):**

  Tule gives your AI agent its own engineering computer. Each project is a Linux
  machine with OpenCascade, Tule's Python modeling API, meshers, FEA and topology
  optimization installed. A web viewer shows what the agent builds, live.

  Ask for a part, such as "a wall hook for my bike, 3D printable". Your agent
  creates a Tule project, writes the model in Python, renders and checks it
  itself, and sends you a viewer link. You can rotate the model, open sections
  and download a print-ready 3MF plate for Bambu Studio, OrcaSlicer or
  PrusaSlicer. Projects are saved in git, so you can come back and iterate.

  With this connector your agent can:
  - create, rename, save and delete projects;
  - start and stop machines: Tule cloud CPUs, GPUs on your RunPod account, or
    your own computer;
  - list each project's outputs with viewer links, and restore earlier autosaves;
  - manage SSH and API keys for working on the machine.

  The modelling itself runs on the machine through the `tule` CLI or SSH,
  which the connector sets up (`info` with what=connect). It works best with
  agents that can run commands, such as Claude with code execution, Claude
  Code, Codex and Cursor.

  You can try it without an account. Sign in with Google to keep your projects.

- **Categories:** Developer tools; Design / Productivity
- **Use cases (Claude portal):** reads and writes

## Example prompts (≤128 chars each)

1. Make me a 3D-printable wall hook for a road bike, and send me the viewer link.
2. Show my Tule projects and what each one has built.
3. Start a 4-core machine for my bracket project and run its FEA check.

## Test cases (ChatGPT: 5 positive, 3 negative)

Positive:

1. **List projects:** "What Tule projects do I have?" calls `info` (what=projects).
   Expected: the projects with machine state and viewer links, or none for a new account.
2. **Create a project:** "Create a Tule project called hook." calls `project`
   (create). Expected: the project is created and a viewer link returned.
3. **Start a machine:** "Start a machine for hook." calls `machine` (start).
   Expected: it waits until `/workspace` is ready, then reports the machine running.
4. **Read the guide:** "How do I model a part with Tule?" calls `guide`
   (index, then modeling). Expected: Tule's modeling instructions.
5. **Stop and clean up:** "Stop hook's machine and delete the project." calls
   `machine` (stop), then `project` (delete, confirm=hook). Expected: stopped, then deleted.

Negative (should not call Tule):

1. "What's the weather in Berlin?"
2. "Write a poem about bicycles."
3. "Convert 3 inches to millimetres."

## Status

- Claude: submitted 16 November 2026 (claude.ai/directory/manage).
- ChatGPT: the plugin package is `openai-plugin/` (zip it as `tule-openai-plugin.zip`
  and upload at platform.openai.com/plugins). Needs a verified developer identity.
  Domain challenge: set `OPENAI_APPS_CHALLENGE` in `control/wrangler.jsonc` and deploy.
  Demo video: `https://tule.computer/demo/tule-chatgpt-demo.mp4` (web/src/demo).
