"""Diagnose why the Cloudflare interstitial is not detected on APKMirror.

The solver reports ``Cloudflare iframes not found``, which has two very
different causes that need opposite fixes:

* the challenge genuinely never renders (we are blocked, no fix in the click);
* the challenge *is* there but the library's shadow-root walk cannot see it (a
  detection fix, and the whole solve follows from it).

This dumps the actual frame tree so the two can be told apart.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from patchright.async_api import async_playwright  # noqa: E402

URL = (
    "https://www.apkmirror.com/apk/pinterest/"
    "pinterest-one-destination-for-a-world-of-inspiration/"
    "pinterest-14-34-0-2-android-apk-download/"
)

CF_SRC = "challenges.cloudflare.com"


async def dump(page, label):
    frames = [f for f in page.frames]
    print(f"\n--- {label} ---")
    print(f"  url: {page.url[:110]}")
    print(f"  frames: {len(frames)}")
    for frame in frames:
        print(f"    [{frame.name or '-'}] {frame.url[:100]}")

    # Every iframe the DOM knows about, including cross-origin ones.
    iframes = await page.evaluate(
        """() => Array.from(document.querySelectorAll('iframe')).map(f => ({
            src: f.src || '',
            id: f.id || '',
            name: f.name || '',
            title: f.title || '',
            w: f.offsetWidth, h: f.offsetHeight,
            display: getComputedStyle(f).display,
        }))"""
    )
    print(f"  <iframe> elements: {len(iframes)}")
    for item in iframes:
        flag = "  <== CLOUDFLARE" if CF_SRC in item["src"] else ""
        print(
            f"    id={item['id']!r} name={item['name']!r} "
            f"{item['w']}x{item['h']} display={item['display']} "
            f"src={item['src'][:80]}{flag}"
        )

    body = await page.evaluate("() => document.body ? document.body.innerText : ''")
    print(f"  body text (first 300): {body[:300]!r}")

    has_cf = any(CF_SRC in item["src"] for item in iframes)
    cf_frames = [f for f in frames if CF_SRC in f.url]
    print(f"  => cloudflare iframe in DOM: {has_cf}")
    print(f"  => cloudflare frame attached: {len(cf_frames)}")
    return has_cf, len(cf_frames)


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=False, args=["--no-sandbox"]
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768}, locale="en-US"
        )
        page = await context.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        await dump(page, "immediately after domcontentloaded")

        # Cloudflare injects its widget after a delay, and the widget cycles
        # through spinner -> checkbox -> spinner, so a single sample proves
        # nothing. Poll and report every time the shape changes.
        print("\n=== polling 40s for the challenge to appear/change ===")
        previous = None
        found_dom = found_frame = False
        for tick in range(40):
            await page.wait_for_timeout(1000)
            has_dom, n_frames = await dump(page, f"t+{tick + 1}s") if (
                tick % 5 == 0
            ) else (False, 0)
            if not has_dom and tick % 5 == 0:
                iframes = await page.evaluate(
                    "() => document.querySelectorAll('iframe').length"
                )
                text = await page.evaluate(
                    "() => (document.title || '') + ' | ' +"
                    " (document.body ? document.body.innerText.slice(0,120) : '')"
                )
                state = (iframes, text)
                if state != previous:
                    print(f"  [t+{tick + 1}s] iframes={iframes} {text[:120]!r}")
                    previous = state
            if has_dom or n_frames:
                found_dom = found_dom or has_dom
                found_frame = found_frame or bool(n_frames)

        print("\n=== result ===")
        print(f"  ever saw CF iframe in DOM: {found_dom}")
        print(f"  ever saw CF frame attached: {found_frame}")
        print(f"  final url: {page.url[:140]}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
