"""Make Cloudflare's sealed shadow root traversable before the widget renders.

The problem
-----------
Cloudflare's interstitial mounts its challenge widget inside a *closed* shadow
root, so ``document.querySelectorAll('iframe')`` finds nothing while
``page.frames`` clearly shows an attached
``challenges.cloudflare.com/cdn-cgi/challenge-platform/...`` frame. Anything
that searches the DOM for the challenge therefore comes up empty and reports
``Cloudflare iframes not found``.

The fix is to rewrite ``Element.prototype.attachShadow`` so it always creates an
*open* root, which has to be installed before the widget mounts.

Why this module exists
----------------------
``playwright-captcha`` tries to install that patch itself and reports success,
but it does not actually run under patchright:

* every ``add_init_script`` variant (context, page, CDP) is a no-op there, and
* ``page.evaluate`` defaults to ``isolated_context=True`` under patchright, so
  an ordinary evaluate patches an isolated world the page cannot see.

Both were verified directly, and the library logs "Injected unlockShadowRoot.js
via CDP for patchright" while ``window._shadowRootPatched`` stays false. The
library's own call sites then pass ``isolated_context=False``, which is the tell
that the main world is the only place this needs to land.

So: patch the main world explicitly, right after navigation and before the
widget mounts, and verify it took effect rather than trusting the injection.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from typing import Any

# Mirrors playwright-captcha's patches/unlockShadowRoot.js, plus a marker so the
# result can be checked instead of assumed.
UNLOCK_SHADOW_ROOT_JS = """
(() => {
  if (window._shadowRootPatched) return 'already';
  window._shadowRootPatched = true;
  const shadowRoots = new WeakMap();
  const originalAttachShadow = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function (init) {
    const root = originalAttachShadow.call(this, { ...init, mode: 'open' });
    shadowRoots.set(this, root);
    return root;
  };
  const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'shadowRoot');
  if (descriptor && descriptor.get) {
    const originalGetter = descriptor.get;
    Object.defineProperty(Element.prototype, 'shadowRoot', {
      get: function () {
        return originalGetter.call(this) || shadowRoots.get(this);
      },
      configurable: true,
      enumerable: descriptor.enumerable,
    });
  }
  return 'installed';
})();
"""

# Reports whether the patch is live in the page's own world.
CHECK_PATCHED_JS = "() => !!window._shadowRootPatched"


# How long to keep looking for the challenge iframe. The interstitial mounts its
# widget a second or two after navigation and can replace it, so this is
# deliberately patient; it is only spent when nothing is found at all.
IFRAME_WAIT_SECONDS = 20.0


async def _evaluate_main_world(page: Any, expression: str) -> Any:
    """Run ``expression`` in the page's main world.

    patchright's ``evaluate`` takes an ``isolated_context`` keyword that defaults
    to True; without forcing it off the patch lands somewhere the page never
    reads. Plain Playwright has no such parameter, so it is only passed when the
    driver accepts it.
    """
    try:
        return await page.evaluate(expression, isolated_context=False)
    except TypeError:
        return await page.evaluate(expression)


async def is_patched(page: Any) -> bool:
    try:
        return bool(await _evaluate_main_world(page, CHECK_PATCHED_JS))
    except Exception:  # noqa: BLE001 - a failed check is just "not yet"
        return False


async def unlock_closed_shadow_roots(page: Any, framework: str) -> bool:
    """Install the open-shadow-root patch on ``page``.

    Registers an init script as well where the driver honours one, so the patch
    is already in place for the *next* navigation. Returns whether the patch is
    live right now, which for patchright depends on the explicit main-world
    evaluate below having run after the current document loaded.
    """
    if framework != "patchright":
        # Plain Playwright honours init scripts, which is both effective and
        # early enough to beat the widget.
        try:
            await page.add_init_script(UNLOCK_SHADOW_ROOT_JS)
        except Exception:  # noqa: BLE001 - fall through to the explicit patch
            pass

    if await is_patched(page):
        return True

    try:
        await _evaluate_main_world(page, UNLOCK_SHADOW_ROOT_JS)
    except Exception:  # noqa: BLE001 - caller re-checks
        return False
    return await is_patched(page)


async def challenge_iframe_count(page: Any) -> int:
    """How many Cloudflare challenge iframes are reachable from the DOM.

    Zero here while ``page.frames`` shows a challenge frame is the exact symptom
    of a still-sealed shadow root, which makes this the check that matters after
    navigating.
    """
    js = """
    () => {
      let count = 0;
      const seen = new Set();
      (function walk(node) {
        if (!node || seen.has(node)) return;
        seen.add(node);
        if (node.shadowRoot) walk(node.shadowRoot);
        const all = node.querySelectorAll ? node.querySelectorAll('*') : [];
        for (const el of all) {
          if (el.shadowRoot) walk(el);
          if (el.tagName === 'IFRAME' && (el.src || '').includes('challenges.cloudflare.com')) {
            count++;
          }
        }
      })(document);
      return count;
    }
    """
    try:
        return int(await _evaluate_main_world(page, js) or 0)
    except Exception:  # noqa: BLE001
        return 0


async def ensure_challenge_reachable(
    page: Any,
    framework: str,
    *,
    attempts: int = 8,
    delay_ms: int = 400,
) -> bool:
    """Patch, then wait for the challenge iframe to become reachable.

    Returns True once the iframe is visible to a DOM search. The patch is
    re-applied between attempts because the widget can mount before the first
    evaluate lands, and a root sealed before the patch exists stays sealed.
    """
    for _ in range(max(1, attempts)):
        await unlock_closed_shadow_roots(page, framework)
        if await challenge_iframe_count(page) > 0:
            return True
        await page.wait_for_timeout(delay_ms)
    # Last word: is the patch at least live? A missing iframe may simply mean
    # the challenge auto-verified away, which is a pass, not a failure.
    return await is_patched(page)


class ShadowKeepalive:
    """Keep the patch applied to every document that appears during a solve.

    Two things make a one-shot injection insufficient. The challenge iframe is
    a *separate document* with its own ``attachShadow``, so patching the top
    page does nothing for the checkbox that actually lives inside it. And the
    widget mounts some time after navigation, so a patch applied too early is
    installed but a patch applied too late finds the root already sealed.

    So documents are patched as they appear. Child frames are patched once, the
    first time they are seen: their document is created once, and repeatedly
    evaluating into a frame that is detaching is both wasteful and enough to
    destabilise the browser driver. The main frame is re-checked each tick
    because navigation replaces its document underneath us.
    """

    def __init__(self, page: Any, framework: str, interval_ms: int = 250) -> None:
        self.page = page
        self.framework = framework
        self.interval_ms = interval_ms
        self._task: Any = None
        self._patched_children: set[int] = set()
        self._failures = 0

    async def _patch_once(self, frame: Any) -> None:
        try:
            await _evaluate_main_world(frame, UNLOCK_SHADOW_ROOT_JS)
        except Exception:  # noqa: BLE001 - frame may be detaching
            self._failures += 1

    async def _run(self) -> None:
        while True:
            try:
                frames = list(self.page.frames)
                main = self.page.main_frame
                for frame in frames:
                    if frame is main:
                        # New document after every navigation.
                        await self._patch_once(frame)
                        continue
                    key = id(frame)
                    if key in self._patched_children:
                        continue
                    try:
                        if frame.is_detached():
                            self._patched_children.discard(key)
                            continue
                    except Exception:  # noqa: BLE001
                        self._patched_children.discard(key)
                        continue
                    await self._patch_once(frame)
                    self._patched_children.add(key)

                if self.framework != "patchright":
                    # Init scripts already cover future documents here.
                    break
                await self.page.wait_for_timeout(self.interval_ms)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - back off rather than spin
                self._failures += 1
                await asyncio.sleep(min(1.0, 0.1 * self._failures))

    async def __aenter__(self) -> "ShadowKeepalive":
        await unlock_closed_shadow_roots(self.page, self.framework)
        self._task = asyncio.ensure_future(self._run())
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass
        self._task = None


# Plain Playwright selectors. Once the roots are open, Playwright's CSS engine
# pierces them on its own, so the library's hand-rolled shadow-root walk (which
# loses the handle at the ShadowRoot -> ElementHandle step) is unnecessary.
#
# The signatures below copy the two functions being replaced exactly, including
# the parameter *order*. The iframe search is called with keyword arguments
# while the element search is called positionally as
# ``(framework, queryable, selector)``, so a reordered signature silently
# receives the framework enum where the frame belongs.


async def _plain_iframe_frames(
    framework: Any, captcha_container: Any, src_filter: str
) -> Any:
    """Poll for the challenge iframe instead of looking once.

    The replaced implementation waited on a selector per shadow root, which gave
    it up to ten seconds for the widget to mount. A bare ``query_selector_all``
    looks exactly once and returns nothing while the page is still building the
    challenge, which is most of the time. Polling restores that tolerance; the
    window is generous because the interstitial mounts the widget a couple of
    seconds after navigation and can re-render it.
    """
    del framework
    deadline = time.monotonic() + IFRAME_WAIT_SECONDS
    while True:
        frames = []
        try:
            elements = await captcha_container.query_selector_all("iframe")
        except Exception:  # noqa: BLE001 - page may be mid-navigation
            elements = []
        for element in elements:
            try:
                src = await element.get_attribute("src") or ""
            except Exception:  # noqa: BLE001
                continue
            if src_filter not in src:
                continue
            try:
                frame = await element.content_frame()
            except Exception:  # noqa: BLE001
                continue
            if frame is not None and not frame.is_detached():
                frames.append(frame)
        if frames or time.monotonic() >= deadline:
            return frames
        await captcha_container.wait_for_timeout(250)


async def _plain_elements(
    framework: Any, queryable: Any, selector: str, timeout: float = 10
) -> Any:
    """Same contract as the replaced search, minus the shadow-root walk.

    The original waited on the selector inside each shadow root; Playwright's
    CSS engine already pierces open roots, so only the waiting has to be kept.
    """
    del framework
    try:
        found = await queryable.query_selector_all(selector)
    except Exception:  # noqa: BLE001 - page may be mid-navigation
        found = []
    if found:
        return found
    deadline = time.monotonic() + max(float(timeout), 1.0)
    while time.monotonic() < deadline:
        try:
            found = await queryable.query_selector_all(selector)
        except Exception:  # noqa: BLE001
            found = []
        if found:
            return found
        await queryable.wait_for_timeout(250)
    return found


@contextmanager
def shadow_lookups(enabled: bool = True) -> Any:
    """Route the library's shadow-DOM lookups through plain CSS selectors.

    Only the lookup strategy changes; the library keeps owning detection,
    clicking and verification. Restored on exit, including on error.
    """
    if not enabled:
        yield False
        return

    from playwright_captcha.solvers.click.cloudflare import solve_by_click
    from playwright_captcha.solvers.click.cloudflare.utils import dom_helpers

    original_iframes = solve_by_click.search_shadow_root_iframes
    original_elements = dom_helpers.search_shadow_root_elements
    solve_by_click.search_shadow_root_iframes = _plain_iframe_frames
    dom_helpers.search_shadow_root_elements = _plain_elements
    try:
        yield True
    finally:
        solve_by_click.search_shadow_root_iframes = original_iframes
        dom_helpers.search_shadow_root_elements = original_elements
