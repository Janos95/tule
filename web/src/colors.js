// Tule's palette at runtime. The colors are defined once, as CSS variables in
// palette.css (the build puts it in front of style.css); what is drawn from JS
// (three.js materials, canvas, inline swatches) reads them here, when it draws.

/** A palette color by variable name: color("--danger-fill") -> "#d4574a". */
export const color = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
