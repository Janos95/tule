// The hosted MCP server at /mcp (Streamable HTTP, JSON responses, stateless),
// for the control plane only, in five tools: guide(topic) with the agent
// instructions (skills/*.md), info (every read), project, machine and keys. The work itself
// happens over SSH with the tule CLI, never through these tools.
//
// Auth: OAuth through this site (the client signs the user in with Google
// and asks for consent) or a Tule API key as a Bearer token; index.ts puts the
// user in ctx.props.userId. JSON-RPC handling follows genau's relay.
import type { User } from "./accounts";
import { accounts } from "./auth";
import * as ops from "./ops";
import { gpuCatalog } from "./runpod";
import { SKILLS } from "./skills.gen";

const MODERN = "2026-07-28";
const LEGACY = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];
const META_VERSION = "io.modelcontextprotocol/protocolVersion";
const SERVER_INFO = { name: "tule", title: "Tule", version: "0.1.0" };

export const INSTRUCTIONS = `Tule runs the user's engineering projects on machines: Linux with OpenCascade, the \`tule\` Python modeling API (immutable shapes, tags that survive operations) and topology optimization. These tools manage the control plane only: info reads everything (projects, a project's machines, outputs, autosaves, how to connect, the user's computers and GPUs, keys); project, machine and keys change things. Machines: cpu-1 = 1 vCPU/6 GiB, cpu-2, cpu-4; GPUs on the user's RunPod account; the user's own computers. A project can be loaded on several machines at once, each with its own copy of /workspace. The work happens on a machine over SSH with the tule CLI (info what=connect says how): src/<name>.py makes /workspace/outputs/<name>/ and \`tule build\` runs the scripts whose inputs changed (nothing runs by itself). The user watches outputs live in the viewer (STEP/STL in 3D, tables, Vega-Lite charts, Markdown); check them with \`tule screenshot\`. Each copy is autosaved continuously; \`tule save\` (or project action=save) commits it to the project's git history and merges what other machines saved. Nothing outside /workspace persists. Read guide("index") first, then the topic you need.`;

type Tool = { name: string; description: string; inputSchema: Record<string, unknown>; annotations: Record<string, unknown> };
const str = (description: string) => ({ type: "string", description });
const choice = (values: string[], description: string) => ({ type: "string", enum: values, description });
const obj = (properties: Record<string, unknown>, required: string[] = []) => ({ type: "object", properties, required, additionalProperties: false });
const PROJECT = str("project name, e.g. camera-bracket");
const MACHINE_ID = str("which machine's copy (an id from info what=project: cloud, gpu-<type>, a computer's name, joined); needed only when several run");
// every hint stated (directories require all of them): reads are read-only; the rest may delete
const hints = (title: string, readOnly: boolean, destructive: boolean, idempotent: boolean) =>
  ({ title, readOnlyHint: readOnly, destructiveHint: destructive, idempotentHint: idempotent, openWorldHint: false });

const INFO = ["projects", "project", "outputs", "snapshots", "connect", "resources", "keys"] as const;
const PROJECT_ACTIONS = ["create", "rename", "delete", "save", "join"] as const;
const MACHINE_ACTIONS = ["start", "stop", "forget", "snapshot", "restore"] as const;
const KEY_ACTIONS = ["add_ssh", "remove_ssh", "create_api", "revoke_api"] as const;

export const TOOLS: Tool[] = [
  {
    name: "guide",
    description: "Tule's instructions for agents by topic. Start with 'index'.",
    inputSchema: obj({ topic: str(`one of: ${Object.keys(SKILLS).join(", ") || "index"}`) }),
    annotations: hints("Read Tule's guide", true, false, true),
  },
  {
    name: "info",
    description: [
      "Read anything; changes nothing. what=",
      "projects (default): the user's projects with machine state and viewer links.",
      "project: one project's machines (state, size, unsaved work, timings, SSH sessions) and its latest save.",
      "outputs: what the viewer shows (outputs/, work/, inputs/ with files and viewer links); a stale output says why, a failed one has its error and log.",
      "snapshots: autosaves of /workspace, newest first (limit, machine).",
      "connect: how to work on the project's machine: one command that installs the tule CLI and signs it in (no user step), then SSH; the viewer link.",
      "resources: where projects can run: this month's machine hours and GPU cost, the user's own computers (tule host up), and GPUs on their RunPod account with prices.",
      "keys: the user's SSH keys and Tule API keys (prefixes only).",
    ].join(" "),
    inputSchema: obj({
      what: choice([...INFO], "what to read (default: projects)"),
      project: PROJECT,
      machine: str("snapshots: only this machine's copy"),
      limit: { type: "number", description: "snapshots: how many (default 20)" },
    }),
    annotations: hints("Read projects, machines and account", true, false, true),
  },
  {
    name: "project",
    description: [
      "Change a project. action=",
      "create (name): a new /workspace from the template (AGENTS.md, tule.toml, src/part.py, git).",
      "rename (project, to).",
      "delete (project, confirm = the name again): stops its machines and deletes every copy of /workspace, its saves and autosaves. Cannot be undone; confirm with the user first.",
      "save (project, message, machine): commit a machine's /workspace, merge what other machines saved and push it as the latest save. On a merge conflict nothing is saved: the answer lists the files; resolve them on that machine and save again.",
      "join (project): a join token for a machine started elsewhere (e.g. a pod made with the user's own RunPod connector): run the Tule image with these environment variables and it joins the project.",
    ].join(" "),
    inputSchema: obj({
      action: choice([...PROJECT_ACTIONS], "what to do"),
      project: PROJECT,
      name: str("create: lowercase letters, digits and '-', e.g. camera-bracket"),
      to: str("rename: the new name"),
      confirm: str("delete: the project name again"),
      message: str("save: what changed"),
      machine: MACHINE_ID,
    }, ["action"]),
    annotations: hints("Create, save or delete a project", false, true, false),
  },
  {
    name: "machine",
    description: [
      "Run a project on machines. action=",
      "start (project, machine): load the project on a machine and wait until its /workspace is ready (a machine it ran on before gets its own copy back; a new one starts from the latest save). Other machines keep running. cpu-1 (default, 1 vCPU / 6 GiB), cpu-2 (2 / 8), cpu-4 (4 / 12), or a GPU on the user's RunPod account: gpu-rtx4090, gpu-l40s, gpu-a100, gpu-h100, ... (info what=resources has prices; GPU time costs the user money, so ask first), or one of the user's own computers by name. The project has one Tule cloud machine: another cpu size resizes it.",
      "stop (project, machine = one id, default all): autosave and stop. Unsaved work stays in that machine's copy.",
      "forget (project, machine): drop a stopped machine's copy of /workspace with its unsaved work. Confirm with the user first.",
      "snapshot (project, machine): autosave now (it must be running).",
      "restore (project, id, machine = restore into this one, default the one that made it): make /workspace match an autosave; the current state is autosaved first, so this can be undone.",
    ].join(" "),
    inputSchema: obj({
      action: choice([...MACHINE_ACTIONS], "what to do"),
      project: PROJECT,
      machine: str("start: cpu-1, cpu-2, cpu-4, gpu-<type> or a computer's name; otherwise a machine's id from info what=project"),
      id: str("restore: a snapshot id from info what=snapshots"),
    }, ["action", "project"]),
    annotations: hints("Start, stop or restore a machine", false, true, false),
  },
  {
    name: "keys",
    description: "Change the user's keys (info what=keys lists them). action= add_ssh (key = the public key line, name): an SSH key for every project's machine. remove_ssh (id). create_api (name = what it is for): a Tule API key, shown once, e.g. for CI (the CLI gets its own with `tule login`). revoke_api (id).",
    inputSchema: obj({
      action: choice([...KEY_ACTIONS], "what to do"),
      key: str("add_ssh: the public key line (ssh-ed25519 ...)"),
      name: str("add_ssh / create_api: a label"),
      id: str("remove_ssh / revoke_api: the key's id"),
    }, ["action"]),
    annotations: hints("Add or revoke keys", false, true, false),
  },
];
const NAMES = new Set(TOOLS.map((t) => t.name));

const need = (a: Record<string, unknown>, what: string, ...fields: string[]): ops.Result | null => {
  const missing = fields.filter((f) => a[f] === undefined || a[f] === "");
  return missing.length ? { error: `${what} needs ${missing.join(", ")}` } : null;
};

async function startAndWait(env: Env, user: User, a: Record<string, unknown>, origin: string): Promise<ops.Result> {
  const r = await ops.up(env, user, a.project, a.machine, origin);
  if (r.error) return r;
  // wait (bounded) until the machine is ready, so the agent can SSH in next
  const m = String(a.machine || "cpu-1");
  const slot = m.startsWith("cpu-") ? "cloud" : m;
  const ready = (s: ops.Result) => (s.machines as ops.Result[] | undefined)?.find((x) => x.id === slot);
  const p = await ops.resolve(env, user, a.project); // once: the loop asks the project itself
  if (!ops.isRef(p)) return p;
  const status = async (): Promise<ops.Result> => ({ ...(await ops.projectStub(env, p.id).status()), viewer: ops.viewerUrl(origin, p.name) });
  for (let i = 0; i < (m.startsWith("cpu-") ? 100 : 150); i++) {
    const s = await status();
    const x = ready(s);
    if (s.error || x?.state === "running" || (x?.state === "stopped" && x.lastError)) return s;
    await new Promise((res) => setTimeout(res, 600));
  }
  return { ...(await status()), note: "still starting; check info what=project again shortly" };
}

async function info(env: Env, user: User, a: Record<string, unknown>, origin: string): Promise<ops.Result> {
  const acc = accounts(env);
  const what = String(a.what ?? "projects");
  if (["project", "outputs", "snapshots", "connect"].includes(what)) {
    const bad = need(a, `info what=${what}`, "project");
    if (bad) return bad;
  }
  switch (what) {
    case "projects": return ops.listProjects(env, user, origin);
    case "project": return ops.status(env, user, a.project, origin);
    case "outputs": return ops.outputs(env, user, a.project, origin);
    case "snapshots": return ops.snapshots(env, user, a.project, Number(a.limit ?? 20), a.machine);
    case "connect": {
      const p = await ops.resolve(env, user, a.project);
      return ops.isRef(p) ? ops.connectInfo(env, user, origin, p.name) : p;
    }
    case "resources": {
      const key = await acc.runpodKey(user.id);
      const gpus = key ? { gpus: await gpuCatalog(key) } : { gpus: "RunPod is not connected: the user pastes a RunPod API key in Tule's Settings" };
      return { usage: await ops.usage(env, user), computers: await ops.listHosts(env, user), ...gpus };
    }
    case "keys":
      return {
        ssh: (await acc.sshKeys(user.id)).map((k) => ({ id: k.id, name: k.name, key: k.key.slice(0, 40) + "…" })),
        api: await acc.apiKeys(user.id),
      };
  }
  return { error: `info what=${JSON.stringify(what)}: one of ${INFO.join(", ")}` };
}

async function call(env: Env, user: User, name: string, a: Record<string, unknown>, origin: string): Promise<ops.Result | string> {
  const acc = accounts(env);
  const act = String(a.action ?? "");
  switch (name) {
    case "guide": {
      const topic = String(a.topic ?? "index").replace(/\.md$/, "");
      return SKILLS[topic] ?? `No topic ${JSON.stringify(topic)}. Topics: ${Object.keys(SKILLS).join(", ")}`;
    }
    case "info": return info(env, user, a, origin);
    case "project":
      switch (act) {
        case "create": return need(a, "create", "name") ?? ops.createProject(env, user, a.name, origin);
        case "rename": return need(a, "rename", "project", "to") ?? ops.renameProject(env, user, a.project, a.to);
        case "delete":
          if (!a.project || a.confirm !== a.project) return { error: "confirm must repeat the project name", hint: "ask the user first: this deletes /workspace and all snapshots" };
          return ops.deleteProject(env, user, a.project);
        case "save": return need(a, "save", "project") ?? ops.command(env, user, a.project, { t: "save", message: String(a.message ?? "") }, a.machine);
        case "join": return need(a, "join", "project") ?? ops.join(env, user, a.project, origin);
      }
      return { error: `project action=${JSON.stringify(act)}: one of ${PROJECT_ACTIONS.join(", ")}` };
    case "machine":
      switch (act) {
        case "start": return startAndWait(env, user, a, origin);
        case "stop": return ops.down(env, user, a.project, a.machine);
        case "forget": return ops.forget(env, user, a.project, a.machine);
        case "snapshot": return ops.command(env, user, a.project, { t: "snapshot", reason: "agent" }, a.machine);
        case "restore": return need(a, "restore", "id") ?? ops.command(env, user, a.project, { t: "restore", id: String(a.id) }, a.machine);
      }
      return { error: `machine action=${JSON.stringify(act)}: one of ${MACHINE_ACTIONS.join(", ")}` };
    case "keys":
      switch (act) {
        case "add_ssh": return need(a, "add_ssh", "key") ?? ops.addSshKey(env, user, a.key, a.name);
        case "remove_ssh": return need(a, "remove_ssh", "id") ?? ops.removeSshKey(env, user, a.id);
        case "create_api": {
          const k = await acc.createApiKey(user.id, String(a.name ?? "API key"));
          return { ...k, note: "Shown once. Give it to the user; don't store it in the project." };
        }
        case "revoke_api":
          return (await acc.revokeApiKey(user.id, String(a.id ?? ""))) ? { revoked: a.id } : { error: `no API key ${JSON.stringify(a.id)}` };
      }
      return { error: `keys action=${JSON.stringify(act)}: one of ${KEY_ACTIONS.join(", ")}` };
  }
  return { error: `unknown tool ${name}` };
}

type Id = string | number | null;
type Rpc = { jsonrpc?: string; id?: Id; method?: unknown; params?: Record<string, unknown> };
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type, Accept, Authorization, MCP-Protocol-Version, Mcp-Session-Id",
  "Access-Control-Expose-Headers": "Mcp-Session-Id",
};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...CORS } });
const rpcError = (id: Id, code: number, message: string, status = 200) => json({ jsonrpc: "2.0", id, error: { code, message } }, status);

export async function mcp(req: Request, env: Env, userId: string): Promise<Response> {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  if (req.method !== "POST") return new Response("Tule's MCP server (Streamable HTTP): POST JSON-RPC here.\n", { status: 405, headers: { Allow: "POST, OPTIONS", ...CORS } });
  let msg: Rpc;
  try {
    msg = await req.json();
  } catch {
    return rpcError(null, -32700, "Parse error", 400);
  }
  if (!msg || typeof msg !== "object" || Array.isArray(msg)) return rpcError(null, -32600, "Expected one JSON-RPC message", 400);
  if (typeof msg.method !== "string" || msg.id === undefined) return new Response(null, { status: 202, headers: CORS });
  const id = msg.id;
  const params = (msg.params && typeof msg.params === "object" ? msg.params : {}) as Record<string, unknown>;
  const meta = (params._meta ?? {}) as Record<string, unknown>;
  const modern = typeof meta[META_VERSION] === "string";
  const ok = (result: Record<string, unknown>) => json({ jsonrpc: "2.0", id, result: { resultType: "complete", ...result } });
  // the pages stop showing "Connect your agent": on the first call any client makes (older
  // clients start with initialize, newer ones with server/discover; tools/list either way)
  if (["initialize", "server/discover", "tools/list"].includes(msg.method)) await accounts(env).agentConnected(userId);
  switch (msg.method) {
    case "initialize": {
      const asked = params.protocolVersion;
      const version = typeof asked === "string" && LEGACY.includes(asked) ? asked : LEGACY[0];
      return ok({ protocolVersion: version, capabilities: { tools: { listChanged: false } }, serverInfo: SERVER_INFO, instructions: INSTRUCTIONS });
    }
    case "server/discover":
      return ok({ supportedVersions: [MODERN], capabilities: { tools: {} }, instructions: INSTRUCTIONS, ttlMs: 3_600_000, _meta: { "io.modelcontextprotocol/serverInfo": SERVER_INFO } });
    case "ping":
      return ok({});
    case "tools/list":
      return ok({ tools: TOOLS, ttlMs: 3_600_000 });
    case "tools/call": {
      const name = params.name;
      const args = (params.arguments ?? {}) as Record<string, unknown>;
      if (typeof name !== "string" || !NAMES.has(name)) return rpcError(id, -32602, `Unknown tool: ${String(name)}`);
      const user = await accounts(env).user(userId);
      if (!user) return ok({ content: [{ type: "text", text: "This connection's account no longer exists; reconnect the Tule connector." }], isError: true });
      if (user.guest) await accounts(env).seen(user.id); // a guest whose agent works here is still here
      const origin = env.PUBLIC_URL;
      const t0 = Date.now();
      let out: ops.Result | string;
      try {
        out = await call(env, user, name, args, origin);
      } catch (e) {
        out = { error: `Tule failed: ${e instanceof Error ? e.message : String(e)}` };
      }
      const isError = typeof out !== "string" && !!out.error;
      console.log(JSON.stringify({ event: "tools/call", tool: name, ms: Date.now() - t0, isError }));
      return ok({ content: [{ type: "text", text: typeof out === "string" ? out : JSON.stringify(out, null, 2) }], ...(isError ? { isError: true } : {}) });
    }
    default:
      return rpcError(id, -32601, `Method not found: ${msg.method}`, modern ? 404 : 200);
  }
}
