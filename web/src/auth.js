// The page's credentials, in one place: every request to the control plane
// (the viewer's WebSocket and the REST calls) goes through here.
//
// A signed-in browser carries the session cookie (tule_session, HttpOnly),
// which same-origin fetches and the WebSocket send by themselves. Signed out,
// /api/me answers 401 and the page goes to Google sign-in, coming back here.
// For local tests a dev token still works: it comes once in the link's
// fragment (#t=<token>), is kept in localStorage, and is then sent as
// "Authorization: Bearer" and as ?t= on the WebSocket.
import { store } from "./store.js";

const frag = new URLSearchParams(location.hash.slice(1));
if (frag.get("t")) {
  store.setItem("tule/token", frag.get("t"));
  history.replaceState(null, "", location.pathname + location.search);
}
const token = store.getItem("tule/token");
const headers = () => (token ? { Authorization: `Bearer ${token}` } : {});

export const auth = {
  /** Whether the page may call the API; signed out, it goes to the sign-in page (and resolves false). */
  async signedIn() {
    let r;
    try {
      r = await fetch("/api/me", { headers: headers() });
    } catch {
      return true; // offline: the WebSocket retries and says so
    }
    if (r.status !== 401) return true;
    location.href = `/auth/login?next=${encodeURIComponent(location.pathname + location.search)}`;
    return false;
  },
  /** The WebSocket URL for an API path ("/api/projects/<p>/view"). */
  socket(path) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}${path}${token ? `?t=${encodeURIComponent(token)}` : ""}`;
  },
  /** fetch() for an API path, authorized; throws Error("<error>: <hint>") on a non-2xx answer. */
  async api(path, init = {}) {
    const r = await fetch(path, { ...init, headers: { ...init.headers, ...headers() } });
    if (!r.ok) {
      let msg = `${r.status} ${r.statusText}`;
      try {
        const b = await r.json();
        msg = [b.error, b.hint].filter(Boolean).join(": ") || msg;
      } catch {
        // not JSON
      }
      throw new Error(msg);
    }
    return r;
  },
};
