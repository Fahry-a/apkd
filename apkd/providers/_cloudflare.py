"""Cloudflare challenge solving with a real browser via ``playwright-captcha``.

Why this exists
---------------
``requests``/``curl_cffi`` can scrape most of APKMirror, but APKMirror
intermittently answers with a Cloudflare managed challenge ("Enable JavaScript
and cookies to continue") that no amount of TLS impersonation gets past. The
challenge is a *browser* problem, so the fix is a real browser: open the page,
let ``playwright-captcha``'s Click Solver click the checkbox / panel, then
harvest the resulting ``cf_clearance`` cookie and User-Agent and replay them
through the normal HTTP session.

Design notes
------------
* Click Solver Process (per playwright-captcha's README) is Find -> Click ->
  Wait. The library owns all three steps; this module only drives the browser
  and collects the result.
* ``playwright-captcha`` is **async only** (it imports
  ``playwright.async_api``), while the providers are plain synchronous code, so
  the async work is run to completion in a private event loop via
  :func:`asyncio.run`. The callers are never inside a running loop, so this is
  safe.
* Stealth matters. Standard Playwright Chromium is fingerprinted, Cloudflare
  never renders the challenge iframe, and the solve fails with
  ``CaptchaDetectionError: Cloudflare iframes not found``. ``patchright`` is a
  drop-in stealth fork, so it is preferred and auto-detected here; plain
  ``playwright`` remains available as a fallback.
* Everything is imported lazily so that neither ``playwright`` nor
  ``playwright-captcha`` becomes a hard dependency of the package: without
  them the module raises a clear, actionable :class:`ProviderError`.

Only ever used for pages the caller is authorised to fetch.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import ProviderError
from ._shadow_unlock import (
    ShadowKeepalive,
    _evaluate_main_world,
    ensure_challenge_reachable,
    shadow_lookups,
)
from ._virtual_mouse import humanised_clicks

# Cloudflare interstitial pages carry this token in the URL until the challenge
# is released; its absence afterwards means the browser got through.
INTERSTITIAL_MARKER = "__cf_chl_rt_tk"

# Browsers the solver can drive. Firefox is included because it is a different
# engine fingerprint from Chromium, so a rotation between them is a cheap way
# to get past a challenge that one engine keeps failing.
BROWSER_CHOICES = ("firefox", "chromium")

# A common desktop size. A non-default viewport is itself a weak signal, and
# record_video_size is Chromium-only, so one size suits both cases.
VIEWPORT = {"width": 1366, "height": 768}

# Only Chromium accepts the flags below; passing them to Firefox is an error.
CHROMIUM_ONLY_ARGS = ("--no-sandbox", "--disable-blink-features=AutomationControlled")

# Cookies worth mentioning when diagnosing a failed run. The whole cookie jar
# is actually replayed, because a browser that sails past without being
# challenged never mints a cf_clearance cookie at all, and picking only
# Cloudflare's cookies would then replay nothing and still get a 403.

# Backends in preference order: stealth first, vanilla as a last resort.
FRAMEWORK_ORDER = ("patchright", "playwright")

FRAMEWORK_LABELS = {
    "patchright": "patchright (stealth)",
    "playwright": "playwright (standard)",
}

# playwright-captcha's CaptchaType members, resolved lazily so a missing
# library produces our own error message rather than an ImportError traceback.
CAPTCHA_TYPE_NAMES = {
    "interstitial": "CLOUDFLARE_INTERSTITIAL",
    "turnstile": "CLOUDFLARE_TURNSTILE",
}

INSTALL_HINT = (
    "Cloudflare browser solving needs a browser backend. Install with "
    "'uv pip install playwright-captcha playwright patchright', then fetch the "
    "browser binary with 'python -m patchright install chromium' (or "
    "'python -m playwright install chromium' if you only use the standard "
    "backend). Note playwright-captcha imports playwright at module level, so "
    "'playwright' itself is required even when patchright drives the browser"
)


@dataclass
class BrowserClearance:
    """Cookies + User-Agent proving the browser passed the challenge."""

    cookies: list[dict[str, Any]] = field(default_factory=list)
    user_agent: str = ""
    final_url: str = ""
    challenge_cleared: bool = False

    def to_session(self, http: Any) -> None:
        """Replay this clearance onto an :class:`~apkd.http.HttpClient`.

        The UA must be swapped as well: Cloudflare ties ``cf_clearance`` to the
        User-Agent that earned it, so a mismatched UA invalidates the cookie.
        """
        if self.user_agent:
            http.session.headers["User-Agent"] = self.user_agent
        for cookie in self.cookies:
            try:
                http.session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/"),
                )
            except Exception:  # noqa: BLE001 - a bad cookie must not abort us
                continue


def available_frameworks() -> list[str]:
    """Backends that can actually be imported right now."""
    import importlib.util

    # playwright-captcha is imported lazily but it needs a browser driver
    # importable; without it every backend is unusable.
    if importlib.util.find_spec("playwright_captcha") is None:
        return []

    found = []
    for name in FRAMEWORK_ORDER:
        try:
            if importlib.util.find_spec(f"{name}.async_api") is not None:
                found.append(name)
        except (ImportError, ValueError):
            continue
    return found


def resolve_framework(preferred: str | None = None, browser: str = "firefox") -> str:
    """Pick a browser backend, honouring an explicit preference.

    ``patchright``'s stealth patches are injected over CDP, which only exists
    in Chromium. Pairing it with Firefox is not merely suboptimal: the
    injection fails and the run continues *unpatched*, silently losing the very
    stealth it was chosen for. So Firefox is pinned to the vanilla ``playwright``
    driver and patchright is reserved for Chromium.

    An explicitly requested backend that cannot work here is an error rather
    than a silent downgrade, so a typo never masquerades as "it just didn't
    work".
    """
    available = available_frameworks()
    if not available:
        raise ProviderError(INSTALL_HINT)

    browser_name = (browser or "firefox").strip().lower()
    if browser_name not in BROWSER_CHOICES:
        raise ProviderError(
            f"unknown browser {browser!r}; choose one of "
            f"{', '.join(BROWSER_CHOICES)}"
        )
    usable = [name for name in available if _framework_supports_browser(name, browser_name)]
    if not usable:
        raise ProviderError(
            f"no installed browser backend can drive {browser_name}",
            provider="apkmirror",
        )

    if preferred:
        wanted = preferred.strip().lower()
        if wanted not in FRAMEWORK_ORDER:
            raise ProviderError(
                f"unknown browser backend {preferred!r}; "
                f"choose one of {', '.join(FRAMEWORK_ORDER)}"
            )
        if wanted not in usable:
            reason = (
                "it is Chromium-only (CDP stealth injection)"
                if not _framework_supports_browser(wanted, browser_name)
                else "it is not installed"
            )
            raise ProviderError(
                f"browser backend {wanted!r} cannot drive {browser_name}: {reason}; "
                f"usable backends: {', '.join(usable)}",
                provider="apkmirror",
            )
        return wanted
    # patchright first on Chromium, plain playwright on Firefox.
    return usable[0]


def _framework_supports_browser(framework: str, browser: str) -> bool:
    return not (framework == "patchright" and browser != "chromium")


def _has_chrome_channel() -> bool:
    from shutil import which

    return bool(which("google-chrome") or which("google-chrome-stable"))


def _challenge_type(name: str) -> Any:
    from playwright_captcha import CaptchaType

    try:
        return getattr(CaptchaType, CAPTCHA_TYPE_NAMES[name])
    except KeyError as exc:
        raise ProviderError(
            f"unknown captcha type {name!r}; "
            f"choose one of {', '.join(CAPTCHA_TYPE_NAMES)}"
        ) from exc


def _framework_enum(name: str) -> Any:
    from playwright_captcha import FrameworkType

    return {
        "patchright": FrameworkType.PATCHRIGHT,
        "playwright": FrameworkType.PLAYWRIGHT,
    }[name]


async def _turnstile_token(page: Any) -> str | None:
    """Read the Turnstile response token, which lives in the page's DOM."""
    try:
        return await page.evaluate(
            """() => {
                const input = document.querySelector(
                    'input[name="cf-turnstile-response"]'
                );
                return input ? input.value : null;
            }"""
        )
    except Exception:  # noqa: BLE001 - token is optional signal only
        return None


async def _solve_async(
    url: str,
    *,
    framework: str,
    browser_name: str,
    captcha_type: str,
    timeout_ms: int,
    headless: bool,
    max_attempts: int,
    attempt_delay: int,
    use_chrome_channel: bool,
    humanised: bool,
    pace: str,
    box_rounds: int,
    video_path: str | None,
    profile_dir: str | None,
) -> BrowserClearance:
    if framework == "patchright":
        from patchright.async_api import async_playwright
    else:
        from playwright.async_api import async_playwright

    click_solver_cls = _import_click_solver()
    captcha = _challenge_type(captcha_type)
    framework_enum = _framework_enum(framework)
    if browser_name not in BROWSER_CHOICES:
        raise ProviderError(
            f"unknown browser {browser_name!r}; choose one of "
            f"{', '.join(BROWSER_CHOICES)}"
        )

    playwright = await async_playwright().start()
    browser = None
    try:
        launch_kwargs: dict[str, Any] = {"headless": headless}
        if browser_name == "chromium":
            # --no-sandbox is required when running as root (containers, CI).
            launch_kwargs["args"] = list(CHROMIUM_ONLY_ARGS)
        if browser_name == "chromium" and framework == "patchright" and use_chrome_channel:
            # patchright is most convincing when driving real Chrome.
            launch_kwargs["channel"] = "chrome"

        browser_type = getattr(playwright, browser_name)
        context_kwargs: dict[str, Any] = {"locale": "en-US"}
        if video_path:
            # Record so the click can be inspected afterwards, the way the
            # playwright-captcha README's demos are. Without this the movement
            # happens too fast to verify by eye.
            context_kwargs["record_video_dir"] = str(Path(video_path).parent)
            context_kwargs["record_video_size"] = VIEWPORT

        if profile_dir:
            # A persistent profile is the one lever that changes Cloudflare's
            # verdict rather than our behaviour: cookies, storage and history
            # persist, so a site that granted trust once can grant it again.
            # A throwaway context starts from zero every single run.
            profile_path = Path(profile_dir)
            profile_path.mkdir(parents=True, exist_ok=True)
            launch_kwargs["args"] = [
                arg
                for arg in launch_kwargs.get("args", [])
                if not arg.startswith("--user-data-dir")
            ]
            context = await browser_type.launch_persistent_context(
                str(profile_path), **launch_kwargs, **context_kwargs
            )
        else:
            browser = await browser_type.launch(**launch_kwargs)
            context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        # The solver must exist before navigation so it can install its hooks.
        async with click_solver_cls(
            framework=framework_enum,
            page=page,
            max_attempts=max_attempts,
            attempt_delay=attempt_delay,
        ) as solver:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            # The widget mounts inside a closed shadow root, so it has to be
            # unsealed before anything can find it. playwright-captcha does this
            # itself and reports success, but under patchright the injection
            # never actually runs (see _shadow_unlock for the details).
            await ensure_challenge_reachable(page, framework)

            # Cloudflare re-shows the checkbox instead of issuing clearance more
            # often than not, and each re-show needs a fresh click. The library
            # presses once and gives up, so drive the rounds here: while the
            # challenge is still on screen, click it again.
            # A list, because the click callback needs to mutate a counter.
            clicks = [0]
            last_error: Exception | None = None
            with humanised_clicks(
                page,
                enabled=humanised,
                pace=pace,
                on_click=lambda: clicks.__setitem__(0, clicks[0] + 1),
            ), shadow_lookups():
                async with ShadowKeepalive(page, framework):
                    for round_no in range(1, max(1, box_rounds) + 1):
                        if round_no > 1:
                            # Re-navigate so each round works on a freshly issued
                            # challenge. Re-clicking a challenge the page already
                            # failed to release just burns the remaining rounds.
                            await _renavigate(page, url, timeout_ms, framework)
                        try:
                            await solver.solve_captcha(
                                captcha_container=page,
                                captcha_type=captcha,
                            )
                            last_error = None
                        except Exception as exc:  # noqa: BLE001 - not necessarily fatal
                            last_error = exc

                        # Ask the page, not the library. A solve that cleared the
                        # interstitial often still raises, because the library
                        # verifies success with the same DOM blindness that
                        # caused the failure in the first place.
                        if await _wait_until_cleared(page, captcha_type, 12000):
                            last_error = None
                            break
                        if round_no >= max(1, box_rounds):
                            break
                        print(
                            f"APKMirror: still challenged after click "
                            f"{clicks[0]} (round {round_no}/{box_rounds}); "
                            f"reloading...",
                            file=sys.stderr,
                        )
                        await _renavigate(page, url, timeout_ms, framework)

            if last_error is not None:
                raise ProviderError(
                    f"{FRAMEWORK_LABELS[framework]} on {browser_name} could not "
                    f"get past the Cloudflare {captcha_type} challenge on {url} "
                    f"after {clicks} click(s) ({type(last_error).__name__}: "
                    f"{last_error})"
                ) from last_error

        # Give the interstitial a moment to release and set cf_clearance.
        cleared = await _wait_for_clearance(page, captcha_type, timeout_ms)

        cookies = await context.cookies()
        try:
            user_agent = await page.evaluate("() => navigator.userAgent")
        except Exception:  # noqa: BLE001 - UA is best-effort
            user_agent = ""
        token = await _turnstile_token(page)
        if token:
            print(
                f"APKMirror browser solve: Turnstile token acquired "
                f"({token[:24]}...)",
                file=sys.stderr,
            )
        return BrowserClearance(
            cookies=cookies,
            user_agent=user_agent,
            final_url=page.url,
            challenge_cleared=cleared,
        )
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001 - teardown must not mask errors
                pass
        try:
            await playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        if video_path:
            await _save_video(page, Path(video_path))


async def _save_video(page: Any, target: Path) -> None:
    """Move the recorded clip to ``target``.

    Playwright only flushes the file when the context closes, and it lands in
    the record dir under a generated name, so the rename has to happen after
    teardown. Best effort: a missing recording must not mask the real result.
    """
    try:
        video = page.video
        if video is None:
            return
        # Video.path() is a coroutine in the async API.
        source = Path(await video.path())
        if not source.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        print(f"APKMirror: solve video saved to {target}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        print(f"APKMirror: could not save the solve video ({exc})", file=sys.stderr)


async def _renavigate(
    page: Any, url: str, timeout_ms: int, framework: str
) -> None:
    """Reload the challenge page, tolerating the page navigating underneath us.

    Cloudflare reloads the interstitial by itself after a click, so a ``goto``
    issued while that is in flight comes back as ``net::ERR_ABORTED``. That is
    not a failure to retry blindly: the reload we wanted is already happening.
    Wait for the page to settle, then confirm we ended up somewhere real.
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001 - only some aborts are benign
        if "ERR_ABORTED" not in str(exc):
            raise
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:  # noqa: BLE001
            pass
    await ensure_challenge_reachable(page, framework)


async def _wait_until_cleared(
    page: Any, captcha_type: str, budget_ms: int
) -> bool:
    """Give the interstitial a moment to release before calling it a failure.

    Cloudflare clears asynchronously: the click releases the challenge, the page
    reloads, and only then is the real content there. Judging the click on the
    instant it returns reads a pass as a failure.
    """
    deadline = budget_ms
    step = 500
    while deadline > 0:
        if not await _challenge_on_screen(page, captcha_type):
            return True
        await page.wait_for_timeout(step)
        deadline -= step
    return not await _challenge_on_screen(page, captcha_type)


async def _challenge_on_screen(page: Any, captcha_type: str) -> bool:
    """Is a Cloudflare challenge still being shown?

    Deliberately *not* delegated to the library's own detector: that detector
    inspects the DOM the same way the shadow root hid things from, so it cannot
    be trusted here. This checks the things that are actually observable — the
    challenge token in the URL, a live challenge iframe (found with a selector,
    which pierces open roots), and the interstitial page's own copy.
    """
    if captcha_type == "turnstile":
        return not await _turnstile_token(page)

    if INTERSTITIAL_MARKER in page.url:
        return True
    try:
        for element in await page.query_selector_all("iframe"):
            src = await element.get_attribute("src") or ""
            if "challenges.cloudflare.com" in src:
                return True
    except Exception:  # noqa: BLE001 - page may be mid-navigation
        pass
    try:
        text = await _evaluate_main_world(
            page, "() => (document.body ? document.body.innerText : '')"
        )
    except Exception:  # noqa: BLE001
        return False
    text = text or ""
    return any(
        marker in text
        for marker in ("Performing security verification", "Verify you are human")
    )


async def _wait_for_clearance(page: Any, captcha_type: str, timeout_ms: int) -> bool:
    """Poll until the challenge is released, so cookies are actually set.

    Solving can return before Cloudflare has issued the clearance cookie, so
    harvesting immediately would silently yield an empty cookie jar.
    """
    deadline_ms = max(timeout_ms, 1_000)
    step_ms = 500
    waited_ms = 0

    while waited_ms < deadline_ms:
        if captcha_type == "turnstile":
            # Turnstile can look solved before the token is written.
            if await _turnstile_token(page):
                return True
        elif INTERSTITIAL_MARKER not in page.url:
            # Interstitial: the challenge token leaves the URL once released.
            return True
        await page.wait_for_timeout(step_ms)
        waited_ms += step_ms
    return False


def _import_click_solver() -> Any:
    try:
        from playwright_captcha import ClickSolver
    except ImportError as exc:
        # playwright-captcha imports playwright.async_api at module level and
        # raises its own ImportError when it is absent, so the real cause is
        # swallowed here; always show the full install instructions.
        raise ProviderError(f"{INSTALL_HINT} (underlying error: {exc})") from exc
    return ClickSolver


def solve_cloudflare_challenge(
    url: str,
    *,
    provider: str = "apkmirror",
    captcha_type: str = "interstitial",
    framework: str | None = None,
    browser: str = "firefox",
    headless: bool = False,
    timeout: float = 60.0,
    max_attempts: int = 5,
    attempt_delay: int = 3,
    humanised: bool = True,
    pace: str = "quick",
    box_rounds: int = 4,
    video_path: str | None = None,
    profile_dir: str | None = None,
) -> BrowserClearance:
    """Pass a Cloudflare challenge in a real browser and return the clearance.

    :param url: the page that is currently challenging us.
    :param captcha_type: ``"interstitial"`` (default) or ``"turnstile"``.
    :param framework: force a backend, otherwise auto-detect (stealth first).
    :param browser: ``"firefox"`` (default) or ``"chromium"``. Firefox is the
        default because its engine fingerprint differs from the automated
        Chromium everyone else drives, and the humanised cursor is a better fit
        for it.
    :param headless: run without a window. Cloudflare usually rejects headless
        sessions outright, so this is for debugging only.
    :param timeout: overall navigation budget in seconds.
    :param humanised: click through a real pointer with jitter and a settle
        pause instead of an instant ``element.click()``.
    :param pace: how the cursor travels. ``"flick"``/``"quick"`` are brisk
        human throws; ``"careful"`` is the same shape stretched out so the
        movement can actually be watched in a headed browser.
    :param box_rounds: how many times to re-click the verification box when
        Cloudflare shows it again instead of issuing clearance.
    :param video_path: record the solve to this ``.webm``. Note that Playwright
        captures page compositing, which excludes the OS pointer, so the
        movement is not visible in the recording.
    :param profile_dir: reuse a persistent browser profile at this path. Trust
        accumulates across runs, which is the only lever here that changes
        Cloudflare's verdict rather than our own behaviour.
    """
    browser_name = (browser or "firefox").strip().lower()
    chosen = resolve_framework(
        framework or os.getenv("APKD_CF_FRAMEWORK") or None,
        browser=browser_name,
    )
    if captcha_type not in CAPTCHA_TYPE_NAMES:
        raise ProviderError(
            f"unknown captcha type {captcha_type!r}; "
            f"choose one of {', '.join(CAPTCHA_TYPE_NAMES)}",
            provider=provider,
        )
    if browser_name not in BROWSER_CHOICES:
        raise ProviderError(
            f"unknown browser {browser!r}; choose one of "
            f"{', '.join(BROWSER_CHOICES)}",
            provider=provider,
        )

    print(
        f"APKMirror: solving the Cloudflare {captcha_type} challenge in "
        f"{browser_name} ({FRAMEWORK_LABELS[chosen]}"
        f"{', headless' if headless else ''}"
        f"{', humanised cursor' if humanised else ''}"
        f"{f' [{pace}]' if humanised and pace not in ('quick', 'normal') else ''}"
        f", up to {box_rounds} click rounds)...",
        file=sys.stderr,
    )

    return asyncio.run(
        _solve_async(
            url,
            framework=chosen,
            browser_name=browser_name,
            captcha_type=captcha_type,
            timeout_ms=int(timeout * 1000),
            headless=headless,
            max_attempts=max_attempts,
            attempt_delay=attempt_delay,
            use_chrome_channel=_has_chrome_channel(),
            humanised=humanised,
            pace=pace,
            box_rounds=box_rounds,
            video_path=video_path or os.getenv("APKD_CF_VIDEO", "") or None,
            profile_dir=profile_dir or os.getenv("APKD_CF_PROFILE", "") or None,
        )
    )
