// A screenshot of a Tule page as a user sees it, once it has settled:
//   node shot.mjs <url> <out.png>
// Uses the installed Chrome (no browser download). Waits for the page to load and for the
// viewer's "Loading…" to go (at most 45 s), so a model that never shows is caught as such.
import { chromium } from "playwright-core";

const [url, out] = process.argv.slice(2);
const browser = await chromium.launch({ channel: "chrome", headless: true, args: ["--enable-unsafe-swiftshader", "--use-angle=swiftshader"] });
try {
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  await page.goto(url, { waitUntil: "load", timeout: 30000 });
  // settled: the outputs are listed (or the page says there are none) and nothing has been loading for 1.5 s
  // (before the list comes there is no "Loading" yet; a sequence starts loading after it)
  const ready = () =>
    !/Loading…|Loading \d+ \/ \d+ files|connecting…|\b0 frames/.test(document.body.innerText) && // the viewer's own words
    !!document.querySelector("#output-list > *, #outputs-empty:not([hidden]), .page-error");
  let settled = false;
  for (let calm = 0, t = Date.now(); Date.now() - t < 45000; await page.waitForTimeout(250)) {
    calm = (await page.evaluate(ready).catch(() => false)) ? calm + 1 : 0;
    if (calm >= 6) {
      settled = true;
      break;
    }
  }
  await page.screenshot({ path: out });
  console.log(JSON.stringify({ settled, errors: errors.slice(0, 10) }));
} finally {
  await browser.close();
}
