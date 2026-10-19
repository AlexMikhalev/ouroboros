# Render, look, measure

Look before you measure. A screenshot is a picture you must actually open; a measurement is evidence, never a verdict.

## Ouroboros, with its own tools

- `browse_page(url, output="screenshot", viewport="956x801", engine="webkit")` renders the page and attaches the pixels to your context in the same round. `file://` paths work. Name the engine you used: WebKit shows what a native macOS window shows but may download on first use; Chromium is bundled.
- `browser_action(action="screenshot")` saves pixels but does not show them to you. Follow it with `view_image(path)` on the saved file, or `analyze_screenshot`, before saying what you saw.
- `browser_action(action="evaluate", value=<the text of measure.js>)` runs the measurement snippet on the current page and returns its facts and findings; the same call with `freeze.js` returns one self-contained HTML string of the rendered page for a comparison frame. Take the screenshot first so the page has painted; WebKit can report one width before the first paint and another after.
- Crops: evaluate `document.querySelector(sel).getBoundingClientRect()` and screenshot again at a larger viewport scale, or view the saved 2× image; judge detail on the crop, never on a stitched or scaled sheet.

## A delegated session, with plain Playwright

- Open the delivered file the way the owner will: copy the lone file into an empty folder and open it from there, so anything it still fetches shows up as a failed request.
- Viewport and engine come from the brief (the viewer's window, device scale factor 2, their engine). `PLAYWRIGHT_BROWSERS_PATH` or the host facts in your instructions name an existing browser cache; make one bounded check for a Python or Node that imports Playwright, and stop there. No disk hunting.
- Screenshot, then read the PNG yourself with your native image tool. Then `page.evaluate` the measurement snippet. Then 2× crops of the compared element.
- A readonly session that cannot run a browser says so and returns the file for the parent to look at. Never write "I looked" about a picture you did not see.

### Python

```python
import json, pathlib, shutil, tempfile
from playwright.sync_api import sync_playwright

src = pathlib.Path("page.html").resolve()
skill = pathlib.Path("<absolute path to skills/ui_design/references>")
tmp = pathlib.Path(tempfile.mkdtemp()); shutil.copy(src, tmp / src.name)   # opened alone, as delivered
with sync_playwright() as pw:
    browser = pw.webkit.launch()                                            # or pw.chromium; say which
    page = browser.new_context(viewport={"width": 956, "height": 801},
                               device_scale_factor=2, color_scheme="light").new_page()
    failed = []; page.on("requestfailed", lambda r: failed.append(r.url))
    page.goto((tmp / src.name).as_uri(), wait_until="load"); page.wait_for_timeout(500)
    page.screenshot(path="first-screen.png")                                # look at this file yourself
    report = page.evaluate(f"({(skill / 'measure.js').read_text()})", {"subject": ".card"})
    page.locator(".card").screenshot(path="subject@2x.png")
    print(json.dumps(report, ensure_ascii=False, indent=1), failed)
    browser.close()
shutil.rmtree(tmp, ignore_errors=True)
```

### Node

```js
const { webkit } = require("playwright");                                  // or chromium; say which
const fs = require("fs"), os = require("os"), path = require("path");
const src = path.resolve("page.html"), tmp = fs.mkdtempSync(path.join(os.tmpdir(), "look-"));
fs.copyFileSync(src, path.join(tmp, path.basename(src)));                   // opened alone, as delivered
const measure = fs.readFileSync("<absolute path to skills/ui_design/references>/measure.js", "utf8");
(async () => {
  const browser = await webkit.launch();
  const page = await browser.newContext({ viewport: { width: 956, height: 801 },
                                          deviceScaleFactor: 2, colorScheme: "light" }).newPage();
  const failed = []; page.on("requestfailed", r => failed.push(r.url()));
  await page.goto("file://" + path.join(tmp, path.basename(src)), { waitUntil: "load" });
  await page.waitForTimeout(500);
  await page.screenshot({ path: "first-screen.png" });                       // look at this file yourself
  const report = await page.evaluate(`(${measure})`, { subject: ".card" });
  await page.locator(".card").screenshot({ path: "subject@2x.png" });
  console.log(JSON.stringify(report, null, 1), failed);
  await browser.close();
})();
```

## What to do with the result

Read the first screen as a stranger: what is the primary thing, what does each box look like it is, is anything clipped, covered or stuck. Then read the findings and keep the ones the viewer would notice. Fix, look once more, stop. Report the engine, the viewport, what you looked at and what you measured, in that order.
