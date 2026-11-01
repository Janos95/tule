// The page's persistent storage (localStorage, or memory where it is blocked).
function memory() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) };
}

export const store = (() => {
  try {
    localStorage.setItem("tule/probe", "1");
    localStorage.removeItem("tule/probe");
    return localStorage;
  } catch {
    return memory();
  }
})();
