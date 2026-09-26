"""Find an init-script injection method that actually runs under patchright.

playwright-captcha reports "Injected unlockShadowRoot.js via CDP" but
``window._shadowRootPatched`` never becomes true, so the challenge iframe stays
sealed in a closed shadow root and the solver reports
``Cloudflare iframes not found``. The injection needs to be verified, not
trusted, so this tries each candidate and reports which one actually lands.

Run it with a plain page first (``--target local``) to check the mechanics
without Cloudflare in the way.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from patchright.async_api import async_playwright  # noqa: E402

CF_URL = (
    "https://www.apkmirror.com/apk/pinterest/"
    "pinterest-one-destination-for-a-world-of-inspiration/"
    "pinterest-14-34-0-2-android-apk-download/"
)

# Same shape as the library's patch, but it also records that it ran.
PROBE_JS = """
(() => {
  if (window._shadowRootPatched) return;
  window._shadowRootPatched = true;
  const shadowRoots = new WeakMap();
  const originalAttachShadow = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function (init) {
    const root = originalAttachShadow.call(this, {...init, mode: 'open'});
    shadowRoots.set(this, root);
    return root;
  };
  const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'shadowRoot');
  if (descriptor && descriptor.get) {
    const originalGetter = descriptor.get;
    Object.defineProperty(Element.prototype, 'shadowRoot', {
      get: function () { return originalGetter.call(this) || shadowRoots.get(this); },
      configurable: true,
      enumerable: descriptor.enumerable,
    });
  }
})();
"""

LOCAL_PAGE = """
<!doctype html><meta charset="utf-8"><title>probe</title>
<div id="host"></div>
<script>
  // A closed shadow root, exactly like the challenge widget's container.
  const root = document.getElementById('host').attachShadow({mode: 'closed'});
  root.innerHTML = '<iframe src="https://challenges.cloudflare.com/probe"></iframe>';
</script>
"""

SEALED_JS = """
() => {
  const host = document.getElementById('host');
  if (!host || !host.shadowRoot) return 'sealed';
  const f = host.shadowRoot.querySelector('iframe');
  return f ? 'open-and-has-iframe' : 'open-no-iframe';
}
"""


async def try_method(playwright, method: str, url: str | None) -> bool:
    """Return True when the probe script actually executed."""
    browser = await playwright.chromium.launch(
        headless=False, args=["--no-sandbox"]
    )
    context = await browser.new_context(
        viewport={"width": 1366, "height": 768}, locale="en-US"
    )
    page = await context.new_page()

    cdp = None
    try:
        if method == "context_init":
            await context.add_init_script(PROBE_JS)
        elif method == "page_init":
            await page.add_init_script(PROBE_JS)
        elif method == "cdp_new_doc":
            cdp = await context.new_cdp_session(page)
            await cdp.send(
                "Page.addScriptToEvaluateOnNewDocument", {"source": PROBE_JS}
            )
        elif method == "cdp_immediate":
            cdp = await context.new_cdp_session(page)
            await cdp.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": PROBE_JS, "runImmediately": True},
            )
        else:
            raise ValueError(method)

        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        else:
            await page.set_content(LOCAL_PAGE)
        await page.wait_for_timeout(3500)

        patched = await page.evaluate("() => !!window._shadowRootPatched")
        detail = ""
        if not url:
            detail = f" sealed-check: {await page.evaluate(SEALED_JS)}"
        if not patched:
            return False
        if url:
            found = await page.evaluate(
                """() => {
                    const hits = [];
                    const seen = new Set();
                    (function walk(node) {
                        if (!node || seen.has(node)) return;
                        seen.add(node);
                        if (node.shadowRoot) walk(node.shadowRoot);
                        const all = node.querySelectorAll ? node.querySelectorAll('*') : [];
                        for (const el of all) {
                            if (el.shadowRoot) walk(el);
                            if (el.tagName === 'IFRAME' && el.src.includes('cloudflare')) {
                                hits.push(el.src);
                            }
                        }
                    })(document);
                    return hits.length;
                }"""
            )
            detail = f" cf-iframes-in-shadow: {found}"
        print(f"    {method}: OK{detail}")
        return True
    except Exception as exc:
        print(f"    {method}: FAILED ({type(exc).__name__}: {exc})")
        return False
    finally:
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception:
                pass
        await browser.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["local", "cloudflare"], default="local")
    args = parser.parse_args()
    url = CF_URL if args.target == "cloudflare" else None

    print(f"target: {args.target}")
    async with async_playwright() as playwright:
        for method in ("context_init", "page_init", "cdp_new_doc", "cdp_immediate"):
            await try_method(playwright, method, url)


if __name__ == "__main__":
    asyncio.run(main())
