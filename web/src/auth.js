// The page's credentials, in one place: every request to the control plane
// (the viewer's WebSocket and the REST calls) goes through here.
//
// Until accounts exist (milestone 4) that is the dev token: it comes once in
// the link's fragment, #t=<token> (tule open), and is kept in localStorage.
// With accounts this becomes the session cookie: socket() and api() drop the
// token and the browser sends the cookie.
import { store } from "./store.js";

const frag = new URLSearchParams(location.hash.slice(1));
if (frag.get("t")) {
  store.setItem("tule/token", frag.get("t"));
  history.replaceState(null, "", location.pathname + location.search);
}
const token = store.getItem("tule/token");

export const auth = {
  /** Why the page cannot connect, or "" when it can. */
  missing: () => (token ? "" : "No access token: open this page with tule open"),
  /** The WebSocket URL for an API path ("/api/projects/<p>/view"). */
  socket(path) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}${path}?t=${encodeURIComponent(token ?? "")}`;
  },
  /** fetch() for an API path, authorized; throws Error(error + hint) on a non-2xx answer. */
  async api(path, init = {}) {
    const r = await fetch(path, { ...init, headers: { ...init.headers, Authorization: `Bearer ${token ?? ""}` } });
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
