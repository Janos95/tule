// The one Accounts Durable Object ("global"): users, browser sessions, API
// keys (the CLI's and the MCP connector's credential), device codes for
// `tule login`, SSH keys, projects (name -> project id per user), the RunPod
// connection (key encrypted with ENCRYPTION_KEY) and usage per month.
//
// Guests: a first visit without an account makes a guest user (no Google
// identity, `guest` = 1) so people can try Tule before signing in. Guests have
// tight limits (GUEST_* vars: projects, machine minutes per day, machines
// running at once site-wide) and are deleted, with their projects, a day
// (GUEST_DAYS) after their last visit; signing in with Google moves a guest's
// projects to the account.
//
// Secrets (session tokens, API keys, device codes) are stored as SHA-256
// hashes only. Project ids ("p_" + random) name the Project Durable Objects;
// users address projects by name.
import { DurableObject } from "cloudflare:workers";

export type User = { id: string; email: string; name: string; guest?: boolean };
export type ProjectRef = { id: string; name: string; created: number };
type Result = Record<string, unknown>;

const SESSION_DAYS = 30;
const DEVICE_MINUTES = 10;
const HOST_KEY_MINUTES = 60;
const LOGIN_MINUTES = 10;

export const rand = (bytes = 24) =>
  btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(bytes)))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

export async function sha256hex(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((x) => x.toString(16).padStart(2, "0")).join("");
}

const month = (t = Date.now()) => new Date(t).toISOString().slice(0, 7);
const day = (t = Date.now()) => new Date(t).toISOString().slice(0, 10);

/** A short code a person types: 8 letters without look-alikes, shown as ABCD-EFGH. */
function userCode(): string {
  const A = "BCDFGHJKLMNPQRSTVWXZ";
  const b = crypto.getRandomValues(new Uint8Array(8));
  const s = [...b].map((x) => A[x % A.length]).join("");
  return `${s.slice(0, 4)}-${s.slice(4)}`;
}

async function aesKey(secret: string): Promise<CryptoKey> {
  const raw = Uint8Array.from(atob(secret), (c) => c.charCodeAt(0));
  if (raw.length !== 32) throw new Error("ENCRYPTION_KEY must be 32 bytes, base64");
  return crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt", "decrypt"]);
}

export class Accounts extends DurableObject<Env> {
  sql: SqlStorage;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, google_sub TEXT UNIQUE, email TEXT, name TEXT, created INTEGER);
      CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, user_id TEXT, created INTEGER, expires INTEGER);
      CREATE TABLE IF NOT EXISTS logins (state TEXT PRIMARY KEY, verifier TEXT, next TEXT, expires INTEGER);
      CREATE TABLE IF NOT EXISTS api_keys (hash TEXT PRIMARY KEY, id TEXT UNIQUE, user_id TEXT, name TEXT, prefix TEXT, created INTEGER, last_used INTEGER);
      CREATE TABLE IF NOT EXISTS devices (hash TEXT PRIMARY KEY, user_code TEXT UNIQUE, client TEXT, expires INTEGER, user_id TEXT);
      CREATE TABLE IF NOT EXISTS ssh_keys (id TEXT PRIMARY KEY, user_id TEXT, key TEXT, name TEXT, created INTEGER, UNIQUE(user_id, key));
      CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, user_id TEXT, name TEXT, created INTEGER, UNIQUE(user_id, name));
      CREATE TABLE IF NOT EXISTS runpod (user_id TEXT PRIMARY KEY, iv TEXT, ct TEXT, updated INTEGER);
      CREATE TABLE IF NOT EXISTS usage (user_id TEXT, month TEXT, kind TEXT, seconds REAL, cost REAL, PRIMARY KEY (user_id, month, kind));
      CREATE TABLE IF NOT EXISTS usage_day (user_id TEXT, day TEXT, seconds REAL, PRIMARY KEY (user_id, day));
      CREATE TABLE IF NOT EXISTS guest_ips (ip TEXT, day TEXT, n INTEGER, PRIMARY KEY (ip, day));
      CREATE TABLE IF NOT EXISTS running (project_id TEXT PRIMARY KEY, user_id TEXT, guest INTEGER, since INTEGER);
      CREATE TABLE IF NOT EXISTS hosts (id TEXT PRIMARY KEY, user_id TEXT, name TEXT, token_hash TEXT UNIQUE, created INTEGER, specs TEXT, UNIQUE(user_id, name));
      CREATE TABLE IF NOT EXISTS host_keys (hash TEXT PRIMARY KEY, user_id TEXT, expires INTEGER);
    `);
    // columns added after the first deploy
    for (const [table, col] of [["users", "guest INTEGER DEFAULT 0"], ["users", "last_seen INTEGER"], ["devices", "kind TEXT"], ["devices", "host_name TEXT"]]) {
      try {
        this.sql.exec(`ALTER TABLE ${table} ADD COLUMN ${col}`);
      } catch {
        // already there
      }
    }
  }

  one<T>(q: string, ...args: unknown[]): T | undefined {
    return this.sql.exec(q, ...args).toArray()[0] as T | undefined;
  }

  all<T>(q: string, ...args: unknown[]): T[] {
    return this.sql.exec(q, ...args).toArray() as T[];
  }

  // ------------------------------------------------------------ users and sessions

  /** A Google account signs in: the user, created on first sign-in. */
  async googleUser(sub: string, email: string, name: string): Promise<User> {
    const u = this.one<User>("SELECT id, email, name FROM users WHERE google_sub = ?", sub);
    if (u) {
      this.sql.exec("UPDATE users SET email = ?, name = ? WHERE id = ?", email, name, u.id);
      return { ...u, email, name };
    }
    const id = `u_${rand(12)}`;
    this.sql.exec("INSERT INTO users (id, google_sub, email, name, created) VALUES (?, ?, ?, ?, ?)", id, sub, email, name, Date.now());
    return { id, email, name };
  }

  user(id: string): User | null {
    const u = this.one<{ id: string; email: string | null; name: string | null; guest: number }>("SELECT id, email, name, guest FROM users WHERE id = ?", id);
    return u ? { id: u.id, email: u.email ?? "guest", name: u.name ?? "Guest", ...(u.guest ? { guest: true } : {}) } : null;
  }

  // ------------------------------------------------------------ guests

  limits() {
    const n = (v: string | undefined, d: number) => (Number.isFinite(Number(v)) && v !== "" && v != null ? Number(v) : d);
    return {
      projects: n(this.env.GUEST_PROJECTS, 1),
      minutesPerDay: n(this.env.GUEST_MINUTES_DAY, 30),
      machines: n(this.env.GUEST_MACHINES, 2),
      days: n(this.env.GUEST_DAYS, 1),
      perIpPerDay: 5,
    };
  }

  /** A new guest for a first visit, or why not (too many from this address today). */
  async createGuest(ip: string): Promise<{ user: User; session: string } | { error: string }> {
    const d = day();
    const seen = this.one<{ n: number }>("SELECT n FROM guest_ips WHERE ip = ? AND day = ?", ip, d)?.n ?? 0;
    if (seen >= this.limits().perIpPerDay) return { error: "too many guest sessions from your network today; sign in instead" };
    this.sql.exec("INSERT INTO guest_ips VALUES (?, ?, 1) ON CONFLICT (ip, day) DO UPDATE SET n = n + 1", ip, d);
    this.sql.exec("DELETE FROM guest_ips WHERE day < ?", d);
    const id = `u_g${rand(12)}`;
    this.sql.exec("INSERT INTO users (id, google_sub, email, name, created, guest, last_seen) VALUES (?, NULL, NULL, NULL, ?, 1, ?)", id, Date.now(), Date.now());
    await this.ctx.storage.setAlarm(Date.now() + 3_600_000);
    return { user: { id, email: "guest", name: "Guest", guest: true }, session: await this.createSession(id) };
  }

  /** A guest was here: their projects live on (at most once an hour). */
  seen(userId: string) {
    this.sql.exec("UPDATE users SET last_seen = ? WHERE id = ? AND guest = 1 AND (last_seen IS NULL OR last_seen < ?)", Date.now(), userId, Date.now() - 3_600_000);
  }

  /** A machine may start: guests within their minutes and the site-wide guest machine count. Records it as running. */
  machineStart(projectId: string, userId: string): { error?: string; hint?: string } {
    const u = this.user(userId);
    if (u?.guest) {
      const L = this.limits();
      const used = this.one<{ seconds: number }>("SELECT seconds FROM usage_day WHERE user_id = ? AND day = ?", userId, day())?.seconds ?? 0;
      if (used >= L.minutesPerDay * 60)
        return { error: `today's guest machine time is used up (${L.minutesPerDay} minutes)`, hint: "sign in to keep working, or come back tomorrow" };
      const others = this.one<{ n: number }>("SELECT COUNT(*) AS n FROM running WHERE guest = 1 AND project_id != ?", projectId)?.n ?? 0;
      if (others >= L.machines) return { error: "all guest machines are busy right now", hint: "sign in, or try again in a few minutes" };
    }
    this.sql.exec("INSERT OR REPLACE INTO running VALUES (?, ?, ?, ?)", projectId, userId, u?.guest ? 1 : 0, Date.now());
    return {};
  }

  machineStopped(projectId: string) {
    this.sql.exec("DELETE FROM running WHERE project_id = ?", projectId);
  }

  /** A guest machine still within today's minutes? (the Project object asks while it runs) */
  guestTimeLeft(userId: string): number {
    const u = this.user(userId);
    if (!u?.guest) return Infinity;
    const used = this.one<{ seconds: number }>("SELECT seconds FROM usage_day WHERE user_id = ? AND day = ?", userId, day())?.seconds ?? 0;
    return this.limits().minutesPerDay * 60 - used;
  }

  /** Signing in from a guest session: the guest's projects and keys move to the account. */
  async mergeGuest(guestId: string, userId: string): Promise<number> {
    const g = this.user(guestId);
    if (!g?.guest || guestId === userId) return 0;
    const moved = this.projects(guestId);
    for (const p of moved) {
      let name = p.name;
      for (let i = 2; this.project(userId, name); i++) name = `${p.name}-${i}`;
      this.sql.exec("UPDATE projects SET user_id = ?, name = ? WHERE id = ?", userId, name, p.id);
      await (this.env.PROJECT.getByName(p.id) as unknown as { adopt(owner: string, name: string): Promise<void> }).adopt(userId, name);
    }
    for (const t of ["api_keys", "ssh_keys", "usage", "usage_day", "running"]) {
      try {
        this.sql.exec(`UPDATE OR IGNORE ${t} SET user_id = ? WHERE user_id = ?`, userId, guestId);
      } catch {
        // a primary-key clash (the same SSH key twice): the account's row stays
      }
    }
    this.deleteUser(guestId);
    return moved.length;
  }

  deleteUser(id: string) {
    for (const t of ["sessions", "api_keys", "devices", "ssh_keys", "projects", "runpod", "usage", "usage_day", "running", "hosts"]) this.sql.exec(`DELETE FROM ${t} WHERE user_id = ?`, id);
    this.sql.exec("DELETE FROM users WHERE id = ?", id);
  }

  async alarm() {
    await this.sweep();
  }

  /** Every hour: guests not seen for GUEST_DAYS lose their projects (machines stopped, snapshots deleted), then themselves. */
  async sweep(): Promise<number> {
    const cutoff = Date.now() - this.limits().days * 86_400_000;
    const old = this.all<{ id: string }>("SELECT id FROM users WHERE guest = 1 AND COALESCE(last_seen, created) < ? LIMIT 50", cutoff);
    for (const { id } of old) {
      for (const p of this.projects(id)) {
        try {
          await (this.env.PROJECT.getByName(p.id) as unknown as { destroyProject(): Promise<unknown> }).destroyProject();
        } catch (e) {
          console.log(JSON.stringify({ event: "guest-sweep", project: p.id, error: String(e) }));
          continue;
        }
      }
      this.deleteUser(id);
    }
    // running rows a crashed machine never cleared (guests cannot run longer than their minutes)
    this.sql.exec("DELETE FROM running WHERE guest = 1 AND since < ?", Date.now() - 2 * 3_600_000);
    if (old.length) console.log(JSON.stringify({ event: "guest-sweep", deleted: old.length }));
    const any = this.one<{ n: number }>("SELECT COUNT(*) AS n FROM users WHERE guest = 1")?.n ?? 0;
    if (any) await this.ctx.storage.setAlarm(Date.now() + 3_600_000);
    return old.length;
  }

  /** The dev token's user (until accounts are the only way in). */
  devUser(): User {
    const u = this.user("u_dev");
    if (u) return u;
    this.sql.exec("INSERT INTO users (id, google_sub, email, name, created) VALUES ('u_dev', NULL, 'dev@tule.local', 'Dev token', ?)", Date.now());
    return { id: "u_dev", email: "dev@tule.local", name: "Dev token" };
  }

  async startLogin(next: string): Promise<{ state: string; verifier: string }> {
    const state = rand(24);
    const verifier = rand(48);
    this.sql.exec("DELETE FROM logins WHERE expires < ?", Date.now());
    this.sql.exec("INSERT INTO logins VALUES (?, ?, ?, ?)", state, verifier, next, Date.now() + LOGIN_MINUTES * 60_000);
    return { state, verifier };
  }

  finishLogin(state: string): { verifier: string; next: string } | null {
    const r = this.one<{ verifier: string; next: string; expires: number }>("SELECT verifier, next, expires FROM logins WHERE state = ?", state);
    this.sql.exec("DELETE FROM logins WHERE state = ?", state);
    return r && r.expires > Date.now() ? { verifier: r.verifier, next: r.next } : null;
  }

  async createSession(userId: string): Promise<string> {
    const token = `ts_${rand(32)}`;
    this.sql.exec("INSERT INTO sessions VALUES (?, ?, ?, ?)", await sha256hex(token), userId, Date.now(), Date.now() + SESSION_DAYS * 86_400_000);
    return token;
  }

  async sessionUser(token: string): Promise<User | null> {
    const r = this.one<{ user_id: string; expires: number }>("SELECT user_id, expires FROM sessions WHERE hash = ?", await sha256hex(token));
    return r && r.expires > Date.now() ? this.user(r.user_id) : null;
  }

  async endSession(token: string) {
    this.sql.exec("DELETE FROM sessions WHERE hash = ?", await sha256hex(token));
  }

  // ------------------------------------------------------------ API keys

  async createApiKey(userId: string, name: string): Promise<{ id: string; key: string }> {
    const key = `tule_${rand(32)}`;
    const id = `k_${rand(9)}`;
    this.sql.exec("INSERT INTO api_keys VALUES (?, ?, ?, ?, ?, ?, NULL)", await sha256hex(key), id, userId, name.slice(0, 80), key.slice(0, 9), Date.now());
    return { id, key };
  }

  async apiKeyUser(key: string): Promise<User | null> {
    const hash = await sha256hex(key);
    const r = this.one<{ user_id: string; last_used: number | null }>("SELECT user_id, last_used FROM api_keys WHERE hash = ?", hash);
    if (!r) return null;
    if (!r.last_used || Date.now() - r.last_used > 3_600_000) this.sql.exec("UPDATE api_keys SET last_used = ? WHERE hash = ?", Date.now(), hash);
    return this.user(r.user_id);
  }

  apiKeys(userId: string) {
    return this.all<{ id: string; name: string; prefix: string; created: number; last_used: number | null }>("SELECT id, name, prefix, created, last_used FROM api_keys WHERE user_id = ? ORDER BY created DESC", userId);
  }

  revokeApiKey(userId: string, id: string): boolean {
    return this.sql.exec("DELETE FROM api_keys WHERE user_id = ? AND id = ?", userId, id).rowsWritten > 0;
  }

  // ------------------------------------------------------------ device codes (tule login)

  /** A device code: for the CLI (an API key once approved), or for a computer to pair as a host (`hostName`). */
  async deviceStart(client: string, hostName?: string): Promise<{ deviceCode: string; userCode: string; expiresIn: number }> {
    const deviceCode = rand(32);
    const code = userCode();
    this.sql.exec("DELETE FROM devices WHERE expires < ?", Date.now());
    this.sql.exec(
      "INSERT INTO devices (hash, user_code, client, expires, user_id, kind, host_name) VALUES (?, ?, ?, ?, NULL, ?, ?)",
      await sha256hex(deviceCode), code, client.slice(0, 120), Date.now() + DEVICE_MINUTES * 60_000, hostName ? "host" : "cli", hostName ?? null,
    );
    return { deviceCode, userCode: code, expiresIn: DEVICE_MINUTES * 60 };
  }

  deviceInfo(code: string): { client: string; host: string | null } | null {
    const r = this.one<{ client: string; expires: number; user_id: string | null; host_name: string | null }>("SELECT client, expires, user_id, host_name FROM devices WHERE user_code = ?", code.toUpperCase());
    return r && r.expires > Date.now() && !r.user_id ? { client: r.client, host: r.host_name } : null;
  }

  deviceApprove(code: string, userId: string): boolean {
    return this.sql.exec("UPDATE devices SET user_id = ? WHERE user_code = ? AND expires > ? AND user_id IS NULL", userId, code.toUpperCase(), Date.now()).rowsWritten > 0;
  }

  /** The CLI polls: "pending", "expired", or a new API key once approved (once). */
  async devicePoll(deviceCode: string): Promise<Result> {
    const hash = await sha256hex(deviceCode);
    const r = this.one<{ user_id: string | null; expires: number; client: string; host_name: string | null }>("SELECT user_id, expires, client, host_name FROM devices WHERE hash = ?", hash);
    if (!r || r.expires < Date.now()) return { status: "expired" };
    if (!r.user_id) return { status: "pending" };
    this.sql.exec("DELETE FROM devices WHERE hash = ?", hash);
    const u = this.user(r.user_id)!;
    if (r.host_name) {
      const h = await this.createHost(r.user_id, r.host_name);
      return { status: "approved", hostId: h.id, hostToken: h.token, name: h.name, email: u.email };
    }
    const { key } = await this.createApiKey(r.user_id, `CLI: ${r.client}`);
    return { status: "approved", apiKey: key, email: u.email };
  }

  // ------------------------------------------------------------ hosts (the user's own computers)

  /** A one-time key that adds one computer to the account: Settings puts it in the install command. */
  async createHostKey(userId: string): Promise<{ key: string; expires: number }> {
    const key = `tulep_${rand(24)}`;
    const expires = Date.now() + HOST_KEY_MINUTES * 60_000;
    this.sql.exec("DELETE FROM host_keys WHERE expires < ?", Date.now());
    this.sql.exec("INSERT INTO host_keys VALUES (?, ?, ?)", await sha256hex(key), userId, expires);
    return { key, expires };
  }

  /** The computer redeems a key (once): it becomes one of the account's computers. */
  async pairHost(key: string, name: string, token?: string): Promise<Result> {
    const hash = await sha256hex(key);
    const r = this.one<{ user_id: string; expires: number }>("SELECT user_id, expires FROM host_keys WHERE hash = ?", hash);
    this.sql.exec("DELETE FROM host_keys WHERE hash = ?", hash);
    const u = r && r.expires > Date.now() ? this.user(r.user_id) : null;
    if (!u) return { error: "this key is used up or expired", hint: "copy a fresh command from Settings → Your computers → Add a computer" };
    // the install command run again on a computer that is already this account's: keep it
    const known = token ? await this.hostByToken(token) : null;
    if (known && known.userId === u.id) return { hostId: known.id, hostToken: token, name: known.name, email: u.email, already: true };
    const h = await this.createHost(u.id, name);
    return { hostId: h.id, hostToken: h.token, name: h.name, email: u.email };
  }

  async createHost(userId: string, wanted: string): Promise<{ id: string; name: string; token: string }> {
    const base = wanted.toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "computer";
    let name = base;
    for (let i = 2; this.one("SELECT 1 FROM hosts WHERE user_id = ? AND name = ?", userId, name); i++) name = `${base}-${i}`;
    const id = `h_${rand(12).toLowerCase().replace(/[^a-z0-9]/g, "0")}`;
    const token = `tuleh_${rand(32)}`;
    this.sql.exec("INSERT INTO hosts VALUES (?, ?, ?, ?, ?, NULL)", id, userId, name, await sha256hex(token), Date.now());
    return { id, name, token };
  }

  /** The computer a host token belongs to. */
  async hostByToken(token: string): Promise<{ id: string; userId: string; name: string } | null> {
    const r = this.one<{ id: string; user_id: string; name: string }>("SELECT id, user_id, name FROM hosts WHERE token_hash = ?", await sha256hex(token));
    return r ? { id: r.id, userId: r.user_id, name: r.name } : null;
  }

  hosts(userId: string): { id: string; name: string; created: number; specs: Record<string, unknown> | null }[] {
    return this.all<{ id: string; name: string; created: number; specs: string | null }>("SELECT id, name, created, specs FROM hosts WHERE user_id = ? ORDER BY created", userId).map((h) => ({ ...h, specs: h.specs ? JSON.parse(h.specs) : null }));
  }

  host(userId: string, name: string): { id: string; name: string } | null {
    return this.one<{ id: string; name: string }>("SELECT id, name FROM hosts WHERE user_id = ? AND name = ?", userId, name) ?? null;
  }

  setHostSpecs(id: string, specs: Record<string, unknown>) {
    this.sql.exec("UPDATE hosts SET specs = ? WHERE id = ?", JSON.stringify(specs).slice(0, 4000), id);
  }

  removeHost(userId: string, id: string): boolean {
    return this.sql.exec("DELETE FROM hosts WHERE user_id = ? AND id = ?", userId, id).rowsWritten > 0;
  }

  // ------------------------------------------------------------ SSH keys

  addSshKey(userId: string, key: string, name: string): Result {
    const k = key.trim().split(/\s+/).slice(0, 2).join(" ");
    if (!/^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]+$/.test(k))
      return { error: "not an SSH public key", hint: "pass the contents of ~/.ssh/id_ed25519.pub (ssh-keygen -t ed25519 makes one)" };
    const comment = key.trim().split(/\s+/).slice(2).join(" ");
    const old = this.one<{ id: string }>("SELECT id FROM ssh_keys WHERE user_id = ? AND key = ?", userId, k);
    if (old) return { id: old.id, added: false };
    const id = `s_${rand(9)}`;
    this.sql.exec("INSERT INTO ssh_keys VALUES (?, ?, ?, ?, ?)", id, userId, k, (name || comment || "key").slice(0, 80), Date.now());
    return { id, added: true };
  }

  sshKeys(userId: string): { id: string; key: string; name: string; created: number }[] {
    return this.all("SELECT id, key, name, created FROM ssh_keys WHERE user_id = ? ORDER BY created", userId);
  }

  removeSshKey(userId: string, id: string): boolean {
    return this.sql.exec("DELETE FROM ssh_keys WHERE user_id = ? AND id = ?", userId, id).rowsWritten > 0;
  }

  // ------------------------------------------------------------ projects

  projects(userId: string): ProjectRef[] {
    return this.all("SELECT id, name, created FROM projects WHERE user_id = ? ORDER BY created DESC", userId);
  }

  project(userId: string, name: string): ProjectRef | null {
    return this.one<ProjectRef>("SELECT id, name, created FROM projects WHERE user_id = ? AND name = ?", userId, name) ?? null;
  }

  createProject(userId: string, name: string): ProjectRef | { error: string; hint?: string } {
    if (this.project(userId, name)) return { error: `a project named ${JSON.stringify(name)} exists`, hint: "pick another name" };
    if (this.user(userId)?.guest && this.projects(userId).length >= this.limits().projects)
      return { error: `guests can have ${this.limits().projects} project${this.limits().projects === 1 ? "" : "s"}`, hint: "sign in to make more" };
    const p = { id: `p_${rand(12).toLowerCase().replace(/[^a-z0-9]/g, "0")}`, name, created: Date.now() };
    this.sql.exec("INSERT INTO projects VALUES (?, ?, ?, ?)", p.id, userId, name, p.created);
    return p;
  }

  renameProject(userId: string, name: string, to: string): Result {
    if (this.project(userId, to)) return { error: `a project named ${JSON.stringify(to)} exists` };
    const n = this.sql.exec("UPDATE projects SET name = ? WHERE user_id = ? AND name = ?", to, userId, name).rowsWritten;
    return n ? { renamed: to } : { error: `no project ${JSON.stringify(name)}` };
  }

  deleteProject(userId: string, name: string): boolean {
    return this.sql.exec("DELETE FROM projects WHERE user_id = ? AND name = ?", userId, name).rowsWritten > 0;
  }

  // ------------------------------------------------------------ RunPod

  async setRunpodKey(userId: string, key: string) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = await crypto.subtle.encrypt(
      { name: "AES-GCM", iv, additionalData: new TextEncoder().encode(userId) },
      await aesKey(this.env.ENCRYPTION_KEY),
      new TextEncoder().encode(key),
    );
    const b64 = (b: ArrayBuffer | Uint8Array) => btoa(String.fromCharCode(...new Uint8Array(b)));
    this.sql.exec("INSERT OR REPLACE INTO runpod VALUES (?, ?, ?, ?)", userId, b64(iv), b64(ct), Date.now());
  }

  /** The decrypted key, for the control plane's own calls to RunPod only. */
  async runpodKey(userId: string): Promise<string | null> {
    const r = this.one<{ iv: string; ct: string }>("SELECT iv, ct FROM runpod WHERE user_id = ?", userId);
    if (!r) return null;
    const un = (s: string) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
    const pt = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: un(r.iv), additionalData: new TextEncoder().encode(userId) },
      await aesKey(this.env.ENCRYPTION_KEY),
      un(r.ct),
    );
    return new TextDecoder().decode(pt);
  }

  hasRunpod(userId: string): { connected: boolean; updated: number | null } {
    const r = this.one<{ updated: number }>("SELECT updated FROM runpod WHERE user_id = ?", userId);
    return { connected: !!r, updated: r?.updated ?? null };
  }

  removeRunpod(userId: string) {
    this.sql.exec("DELETE FROM runpod WHERE user_id = ?", userId);
  }

  // ------------------------------------------------------------ usage

  addUsage(userId: string, kind: "cpu" | "gpu", seconds: number, cost = 0) {
    if (!(seconds > 0)) return;
    this.sql.exec(
      "INSERT INTO usage VALUES (?, ?, ?, ?, ?) ON CONFLICT (user_id, month, kind) DO UPDATE SET seconds = seconds + excluded.seconds, cost = cost + excluded.cost",
      userId, month(), kind, seconds, cost,
    );
    this.sql.exec("INSERT INTO usage_day VALUES (?, ?, ?) ON CONFLICT (user_id, day) DO UPDATE SET seconds = seconds + excluded.seconds", userId, day(), seconds);
  }

  usage(userId: string, m = month()): { month: string; cpuHours: number; gpuHours: number; gpuCost: number } {
    const rows = this.all<{ kind: string; seconds: number; cost: number }>("SELECT kind, seconds, cost FROM usage WHERE user_id = ? AND month = ?", userId, m);
    const get = (k: string) => rows.find((r) => r.kind === k);
    return {
      month: m,
      cpuHours: Math.round(((get("cpu")?.seconds ?? 0) / 3600) * 100) / 100,
      gpuHours: Math.round(((get("gpu")?.seconds ?? 0) / 3600) * 100) / 100,
      gpuCost: Math.round((get("gpu")?.cost ?? 0) * 100) / 100,
    };
  }
}
