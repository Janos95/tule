// Tule's palette at runtime. The colors are defined once, as CSS variables in
// palette.css (the build puts it in front of style.css); what is drawn from JS
// (three.js materials, canvas, inline swatches) reads them here, when it draws.

/** A palette color by variable name: color("--danger-fill") -> "#d4574a". */
export const color = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const lists = new Map();
/** A numbered list of palette colors, read once: colors("--series") -> [--series-1, --series-2, ...] up to the first missing. */
export function colors(prefix) {
  if (lists.has(prefix)) return lists.get(prefix);
  const css = getComputedStyle(document.documentElement);
  const out = [];
  for (let i = 1; ; i++) {
    const v = css.getPropertyValue(`${prefix}-${i}`).trim();
    if (!v) break;
    out.push(v);
  }
  if (out.length) lists.set(prefix, out);
  return out;
}
