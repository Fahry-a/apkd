"""Verify a DOM-based path to the challenge frame and its checkbox.

playwright-captcha reaches the challenge by collecting shadow roots and calling
``as_element()`` on each. If that does not survive the round trip under
patchright, the solver sees no iframes even though one is plainly there. This
walks the DOM directly instead and checks every step the solver would need:
find the iframe element, get its frame, and find the checkbox input inside it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import patchright.async_api as pr  # noqa: E402

from apkd.providers._shadow_unlock import ensure_challenge_reachable  # noqa: E402

URL = (
    "https://www.apkmirror.com/apk/pinterest/"
    "pinterest-one-destination-for-a-world-of-inspiration/"
    "pinterest-14-34-0-2-android-apk-download/"
)
CF = "challenges.cloudflare.com"

FIND_IFRAME_JS = """
() => {
  const seen = new Set();
  let found = null;
  (function walk(node) {
    if (!node || found || seen.has(node)) return;
    seen.add(node);
    if (node.shadowRoot) walk(node.shadowRoot);
    const all = node.querySelectorAll ? node.querySelectorAll('*') : [];
    for (const el of all) {
      if (found) return;
      if (el.shadowRoot) walk(el);
      if (el.tagName === 'IFRAME' && (el.src || '').includes('challenges.cloudflare.com')) {
        found = el;
        return;
      }
    }
  })(document);
  return found;
}
"""


async def probe(page, label):
    handle = await page.evaluate_handle(FIND_IFRAME_JS, isolated_context=False)
    element = handle.as_element()
    print(f"  [{label}] iframe element via DOM walk: {bool(element)}")
    if not element:
        await handle.dispose()
        return None

    frame = await element.content_frame()
    print(f"  [{label}] content_frame(): {frame is not None}")
    if frame is None:
        await handle.dispose()
        return None
    print(f"  [{label}] frame.url: {frame.url[:80]}")
    print(f"  [{label}] frame detached: {frame.is_detached()}")

    # The checkbox the solver ultimately wants.
    checkbox = None
    for selector in ('input[type="checkbox"]', "input[type=checkbox]"):
        try:
            checkbox = await frame.query_selector(selector)
        except Exception as exc:
            print(f"  [{label}] query {selector} error: {type(exc).__name__}")
        if checkbox:
            break
    print(f"  [{label}] checkbox in frame: {bool(checkbox)}")
    if checkbox:
        try:
            print(f"  [{label}] checkbox visible: {await checkbox.is_visible()}")
        except Exception as exc:
            print(f"  [{label}] visibility error: {type(exc).__name__}")
    await handle.dispose()
    return frame


async def main() -> None:
    async with pr.async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=False, args=["--no-sandbox"]
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768}, locale="en-US"
        )
        page = await context.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await ensure_challenge_reachable(page, "patchright")

        for tick in range(10):
            await page.wait_for_timeout(1200)
            frames = [f for f in page.frames if CF in f.url]
            print(f"\nt+{(tick + 1) * 1.2:.1f}s  cf frames={len(frames)}  "
                  f"url={page.url[-40:]}")
            found = await probe(page, f"t{(tick + 1) * 1.2:.1f}s")
            if found is not None:
                print("\n=> challenge frame reachable AND checkbox present")
                break

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
