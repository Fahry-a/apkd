"""Check whether the closed shadow root is actually reachable, and why.

``unlockShadowRoot.js`` rewrites ``Element.prototype.attachShadow`` to force
``mode: 'open'`` so the challenge iframe becomes traversable. playwright-captcha
injects it via CDP for patchright. If that injection did not take effect, the
iframe stays sealed inside a closed shadow root and the solver's search finds
nothing even though the frame is clearly attached to the page.

This checks the three things that have to line up for the search to work:
the patch is installed, closed roots are really open, and the challenge iframe
is reachable from the document.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from patchright.async_api import async_playwright  # noqa: E402
from playwright_captcha import CaptchaType, ClickSolver, FrameworkType  # noqa: E402

URL = (
    "https://www.apkmirror.com/apk/pinterest/"
    "pinterest-one-destination-for-a-world-of-inspiration/"
    "pinterest-14-34-0-2-android-apk-download/"
)

CF = "challenges.cloudflare.com"

# The same traversal the library uses, but it reports *where* it got stuck.
WALK_JS = """
() => {
  const out = {patched: !!window._shadowRootPatched, roots: 0, hosts: [], iframes: []};
  const seen = new Set();
  function walk(node) {
    if (!node || seen.has(node)) return;
    seen.add(node);
    if (node.shadowRoot) {
      out.roots++;
      out.hosts.push(node.tagName ? node.tagName.toLowerCase() : String(node));
      walk(node.shadowRoot);
    }
    const all = node.querySelectorAll ? node.querySelectorAll('*') : [];
    for (const el of all) {
      if (el.shadowRoot) walk(el);
      if (el.tagName === 'IFRAME' && el.src) out.iframes.push(el.src);
    }
  }
  walk(document);
  return out;
}
"""


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=False, args=["--no-sandbox"]
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768}, locale="en-US"
        )
        page = await context.new_page()

        # Same construction order as our solver: build the solver, then navigate.
        async with ClickSolver(
            framework=FrameworkType.PATCHRIGHT,
            page=page,
            max_attempts=1,
            attempt_delay=1,
        ) as solver:
            await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            print(f"solver prepared: {solver is not None}")
            print(f"url: {page.url[:100]}")

            for tick in range(12):
                await page.wait_for_timeout(1000)

                # 1. is the patch installed at all?
                patched = await page.evaluate("() => !!window._shadowRootPatched")
                # 2. what does the traversal see?
                walk = await page.evaluate(WALK_JS)
                frames = [f.url for f in page.frames if CF in f.url]

                print(
                    f"t+{tick + 1}s patched={patched} roots={walk['roots']} "
                    f"shadow_iframes={len(walk['iframes'])} "
                    f"cf_frames={len(frames)}"
                )
                if walk["hosts"]:
                    print(f"    hosts: {walk['hosts'][:6]}")
                if walk["iframes"]:
                    print(f"    iframe srcs: {[s[:70] for s in walk['iframes'][:4]]}")
                if frames and not walk["iframes"]:
                    print(
                        "    => frame attached but NOT reachable from the DOM: "
                        "the shadow root is still sealed"
                    )
                    break
                if walk["iframes"]:
                    print("    => challenge iframe IS reachable")
                    break
                if not patched:
                    print("    => unlockShadowRoot.js was never installed")
                    break

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
