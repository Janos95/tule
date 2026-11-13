// What a signed-in user can do, shared by the REST API (the CLI, the pages)
// and the MCP tools, so both behave the same. Every function answers a plain
// object; failures are {error, hint}.
import { SUPPORT_ID, type ProjectRef, type User } from "./accounts";
import { accounts } from "./auth";
import type { Host } from "./host";
import type { Project } from "./project";
import { stub } from "./rpc";

export type Result = Record<string, unknown>;
export const NAME = /^[a-z0-9][a-z0-9-]{0,62}$/;

const fail = (error: string, hint?: string): Result => ({ error, ...(hint ? { hint } : {}) });

export const projectStub = (env: Env, id: string) => stub<Project>(env.PROJECT, id);

/** The user's project by name, or an error result that says what exists. */
export async function resolve(env: Env, user: User, name: unknown): Promise<ProjectRef | Result> {
  if (typeof name !== "string" || !NAME.test(name))
    return fail(`invalid project name ${JSON.stringify(name)}`, "use lowercase letters, digits and '-', up to 63 characters");
  const p = await accounts(env).project(user.id, name);
  if (p) return p;
  const names = (await accounts(env).projects(user.id)).map((x) => x.name);
  return fail(`no project ${JSON.stringify(name)}`, names.length ? `your projects: ${names.join(", ")}; or create it` : "create it first (tule new, or the projects_create tool)");
}

export const isRef = (r: ProjectRef | Result): r is ProjectRef => typeof (r as ProjectRef).id === "string" && !("error" in r);

export function viewerUrl(origin: string, name: string) {
  return `${origin}/p/${name}`;
}

export async function listProjects(env: Env, user: User, origin: string): Promise<Result> {
  const ps = await accounts(env).projects(user.id);
  const rows = await Promise.all(
    ps.map(async (p) => {
      const s = await projectStub(env, p.id).status();
      const machines = (s.machines as Result[]).map((m) => ({ id: m.id, size: m.size, state: m.state, unsaved: m.unsaved }));
      return { name: p.name, state: s.state, machines, saved: s.saved, created: new Date(p.created).toISOString(), viewer: viewerUrl(origin, p.name) };
    }),
  );
  return { projects: rows };
}

export async function createProject(env: Env, user: User, name: unknown, origin: string): Promise<Result> {
  if (typeof name !== "string" || !NAME.test(name))
    return fail(`invalid project name ${JSON.stringify(name)}`, "use lowercase letters, digits and '-', up to 63 characters, e.g. camera-bracket");
  const p = await accounts(env).createProject(user.id, name);
  if ("error" in p) return p;
  await projectStub(env, p.id).init(p.id, name, user.id);
  return { project: name, viewer: viewerUrl(origin, name), next: `load it on a machine (tule up -p ${name}, or machine_start)` };
}

export async function deleteProject(env: Env, user: User, name: unknown): Promise<Result> {
  const p = await resolve(env, user, name);
  if (!isRef(p)) return p;
  try {
    await projectStub(env, p.id).destroyProject();
  } catch (e) {
    return fail(`${p.name} was not deleted: ${e instanceof Error ? e.message : String(e)}`);
  }
  await accounts(env).deleteProject(user.id, p.name);
  return { deleted: p.name };
}

export async function renameProject(env: Env, user: User, name: unknown, to: unknown): Promise<Result> {
  const p = await resolve(env, user, name);
  if (!isRef(p)) return p;
  if (typeof to !== "string" || !NAME.test(to)) return fail(`invalid new name ${JSON.stringify(to)}`);
  const r = await accounts(env).renameProject(user.id, p.name, to);
  if (!r.error) await projectStub(env, p.id).rename(to);
  return r;
}

export async function status(env: Env, user: User, name: unknown, origin: string): Promise<Result> {
  const p = await resolve(env, user, name);
  if (!isRef(p)) return p;
  return { ...(await projectStub(env, p.id).status()), viewer: viewerUrl(origin, p.name) };
}

export async function up(env: Env, user: User, name: unknown, machine: unknown, origin: string): Promise<Result> {
  const p = await resolve(env, user, name);
  if (!isRef(p)) return p;
  const m = typeof machine === "string" && machine ? machine : "cpu-1";
  return projectStub(env, p.id).up(m, env.MACHINE_URL || origin); // machines reach the site at MACHINE_URL in local dev
}

const machineArg = (m: unknown) => (typeof m === "string" && m ? m : undefined);

export async function down(env: Env, user: User, name: unknown, machine?: unknown): Promise<Result> {
  const p = await resolve(env, user, name);
  if (!isRef(p)) return p;
  return projectStub(env, p.id).down(machineArg(machine));
}

export async function forget(env: Env, user: User, name: unknown, machine: unknown): Promise<Result> {
  const p = await resolve(env, user, name);
  if (!isRef(p)) return p;
  const m = machineArg(machine);
  return m ? projectStub(env, p.id).forget(m) : fail("say which machine's copy to forget", "the ids are in machine_status");
}

export async function join(env: Env, user: User, name: unknown, origin: string): Promise<Result> {
  const p = await resolve(env, user, name);
  if (!isRef(p)) return p;
  const r = await projectStub(env, p.id).join();
  return { ...r, env: { TULE_URL: env.MACHINE_URL || origin, TULE_PROJECT: p.id, TULE_JOIN_TOKEN: r.joinToken } };
}

export async function snapshots(env: Env, user: User, name: unknown, limit = 20, machine?: unknown): Promise<Result> {
  const p = await resolve(env, user, name);
  if (!isRef(p)) return p;
  return projectStub(env, p.id).snapshots(limit, machineArg(machine));
}

export async function command(env: Env, user: User, name: unknown, msg: Result, machine?: unknown): Promise<Result> {
  const p = await resolve(env, user, name);
  if (!isRef(p)) return p;
  return projectStub(env, p.id).command(msg, machineArg(machine));
}

export async function outputs(env: Env, user: User, name: unknown, origin: string): Promise<Result> {
  const p = await resolve(env, user, name);
  if (!isRef(p)) return p;
  type Build = { state: string; why?: string; error?: string; log?: string };
  const list = (await projectStub(env, p.id).outputsIndex()) as { name: string; root?: string; meta?: { kind?: string; title?: string }; files: { file: string }[]; build?: Build }[];
  return {
    viewer: viewerUrl(origin, p.name),
    outputs: list.map((o) => {
      const root = o.root ?? "outputs";
      const models = o.files.filter((f) => /\.(step|stp|stl|ply|obj)$/i.test(f.file)).length;
      const id = root === "outputs" ? o.name : `${root}/${o.name}`; // the viewer's id (web/src/main.js)
      return {
        name: o.name,
        folder: `${root}/${o.name}`,
        kind: o.meta?.kind ?? (models === 0 ? "files" : models === 1 ? "part" : "assembly"),
        title: o.meta?.title ?? null,
        files: o.files.map((f) => f.file),
        // what `tule build --check` says, when it is not up to date: stale (why), building, or failed (error, log)
        ...(o.build && o.build.state !== "ok" ? { build: o.build } : {}),
        viewer: `${viewerUrl(origin, p.name)}?o=${encodeURIComponent(id)}`,
      };
    }),
  };
}

/** SSH keys changed: running machines get the new list. */
export async function refreshKeys(env: Env, user: User) {
  const ps = await accounts(env).projects(user.id);
  await Promise.all(ps.map((p) => projectStub(env, p.id).sendConfig().catch(() => {})));
}

export async function addSshKey(env: Env, user: User, key: unknown, name: unknown = ""): Promise<Result> {
  const r = await accounts(env).addSshKey(user.id, String(key ?? ""), String(name ?? ""));
  if (r.added) await refreshKeys(env, user);
  return r;
}

export async function removeSshKey(env: Env, user: User, id: unknown): Promise<Result> {
  const ok = await accounts(env).removeSshKey(user.id, String(id ?? ""));
  if (ok) await refreshKeys(env, user);
  return ok ? { removed: id } : fail(`no SSH key ${JSON.stringify(id)}`, "list them with ssh_keys_list");
}

export async function connectInfo(env: Env, user: User, origin: string, name: string): Promise<Result> {
  if (user.id === SUPPORT_ID) return fail("the support account signs in with its own key, not a code", "tule login --token <key> (an admin makes it: tule admin support-key)");
  const code = await accounts(env).cliLoginCode(user.id);
  return {
    setup: `curl -fsSL ${origin}/cli.sh | sh -s -- ${code}   # installs the tule CLI and signs it in; once per computer (the code works once, for an hour). Skip it if \`tule ls\` already works.`,
    ssh: `tule ssh -p ${name}            # a shell; tule ssh -p ${name} -- <command> runs one command`,
    sshConfig: `tule ssh --config -p ${name}   # then plain: ssh tule-${name}`,
    viewer: viewerUrl(origin, name),
    note: "The machine starts on demand (tule up, or tule ssh starts it). /workspace is the project and is saved continuously.",
  };
}

export async function usage(env: Env, user: User): Promise<Result> {
  const a = accounts(env);
  return { ...(await a.usage(user.id)), cpuQuotaHours: Number(env.CPU_HOURS_MONTH || 100), runpod: (await a.hasRunpod(user.id)).connected };
}

/** The user's computers with their state, from each Host object. */
export type HostRow = { id: string; name: string; online: boolean; specs: unknown; running: unknown; added: string };

export async function listHosts(env: Env, user: User): Promise<HostRow[]> {
  const hs = await accounts(env).hosts(user.id);
  return Promise.all(
    hs.map(async (h) => {
      const st = await stub<Host>(env.HOST, h.id).status();
      return { id: h.id, name: h.name, online: !!st.online, specs: st.specs ?? h.specs, running: st.running, added: new Date(h.created).toISOString().slice(0, 10) };
    }),
  );
}

export async function removeHost(env: Env, user: User, idOrName: string): Promise<Result> {
  const hs = await accounts(env).hosts(user.id);
  const h = hs.find((x) => x.id === idOrName || x.name === idOrName);
  if (!h) return fail(`no computer ${JSON.stringify(idOrName)}`, "list them with tule hosts");
  await stub<Host>(env.HOST, h.id).forget();
  await accounts(env).removeHost(user.id, h.id);
  return { removed: h.name };
}
