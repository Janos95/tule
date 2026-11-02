// A screenshot of a Tule page as a user sees it, once it has settled:
//   node shot.mjs <url> <out.png>
// Uses the installed Chrome (no browser download). Waits for the page to load and for the
// viewer's "Loading…" to go (at most 30 s), so a model that never shows is caught as such.
import { chromium } from "playwright-core";

const [url, out] = process.argv.slice(2);
const browser = await chromium.launch({ channel: "chrome", headless: true, args: ["--enable-unsafe-swiftshader", "--use-angle=swiftshader"] });
try {
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  await page.goto(url, { waitUntil: "load", timeout: 30000 });
  const settled = await page
    .waitForFunction(() => !/Loading…|connecting/.test(document.body.innerText), null, { timeout: 30000 })
    .then(() => true, () => false);
  await page.waitForTimeout(1500); // the model's first frames
  await page.screenshot({ path: out });
  console.log(JSON.stringify({ settled, errors: errors.slice(0, 10) }));
} finally {
  await browser.close();
}
