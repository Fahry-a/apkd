from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup

from .base import UNIVERSAL_ABIS, Provider, matches_abi, normalize_arch
from ..models import Artifact, DownloadRequest, ProviderError


def _parse_variants(panel_html: str) -> list[dict[str, str]]:
    """Parse the "All variants" panel into variant descriptors.

    Each ``div.variant`` inside ``section.variants`` carries the variant file
    ID in its ``location.href='.../download/{id}-x'`` handler, the container
    kind in ``.v-file``, and the ABI list in the preceding ``<p>``. The
    store-promo header above the section is ignored by scoping to it.
    """
    soup = BeautifulSoup(panel_html or "", "html.parser")
    section = soup.select_one("section.variants") or soup
    variants: list[dict[str, str]] = []
    current_abis = ""
    for node in section.select("p, div.variant"):
        if node.name == "p":
            current_abis = node.get_text(" ", strip=True)
            continue
        onclick = (node.get("onclick") or "") + " " + " ".join(
            (child.get("onclick") or "") for child in node.select("[onclick]")
        )
        match = re.search(r"/download/(\d+)-x", onclick)
        if not match:
            continue
        kind_node = node.select_one(".v-file")
        variants.append(
            {
                "file_id": match.group(1),
                "kind": (kind_node.get_text(strip=True) if kind_node else "").lower(),
                "abis": current_abis,
            }
        )
    return variants


@dataclass(frozen=True)
class UptodownTarget:
    """Resolved Uptodown metadata before the browser download step."""

    app_url: str
    app_id: str
    version: str
    file_id: str
    kind: str
    page_url: str
    only_xapk: str


class UptodownProvider(Provider):
    """Uptodown provider with an explicit, human-assisted browser download.

    Uptodown exposes version metadata without a browser, but current download
    URLs are issued only after an interactive Cloudflare Turnstile flow. The
    provider therefore resolves metadata normally and requires
    ``APKD_UPTODOWN_BROWSER=1`` (or ``browser=True``) before opening a headed
    Chromium session. For browsers that reject Playwright's automation
    fingerprint, ``APKD_UPTODOWN_CDP_URL`` can point at a normal Chrome started
    by the operator with remote debugging; the operator still completes the
    challenge manually. It never uses a third-party CAPTCHA solver.
    """

    name = "uptodown"
    capabilities = {
        "version": True,
        "arch": False,
        "dpi": False,
        "min_sdk": False,
        "browser": True,
    }

    def __init__(
        self,
        http=None,
        browser: bool | None = None,
        cdp_url: str | None = None,
        channel: str | None = None,
        manual_click: bool | None = None,
        headless: bool | None = None,
        initial_wait: float | None = None,
        retry_wait: float | None = None,
        max_attempts: int | None = None,
        response_timeout: float | None = None,
        invisible: bool | None = None,
        invisible_seed: int | None = None,
    ) -> None:
        super().__init__(http)
        self.browser = browser
        self.cdp_url = cdp_url
        self.channel = channel
        self.manual_click = manual_click
        self.headless = headless
        self.initial_wait = initial_wait
        self.retry_wait = retry_wait
        self.max_attempts = max_attempts
        self.response_timeout = response_timeout
        self.invisible = invisible
        self.invisible_seed = invisible_seed

    def resolve_request(self, request: DownloadRequest) -> Artifact:
        target = self.resolve_target(request)
        if not self._browser_enabled():
            raise ProviderError(
                f"Uptodown has exact {target.version} ({target.kind}, "
                f"file_id={target.file_id}), but downloading it requires an "
                "interactive browser Turnstile; set APKD_UPTODOWN_BROWSER=1 "
                "and run locally with a visible browser, or connect to a normal "
                "Chrome with APKD_UPTODOWN_CDP_URL",
                provider=self.name,
            )

        (
            url,
            cookies,
            user_agent,
            referer,
            browser_download_path,
        ) = self._browser_download_url(target)
        self._store_cookies(cookies)
        self.http.session.headers["Referer"] = referer or target.page_url
        if user_agent:
            self.http.session.headers["User-Agent"] = user_agent
        extension = ".xapk" if target.kind == "xapk" else ".apk"
        extra = {
            "app_id": target.app_id,
            "file_id": target.file_id,
            "kind": target.kind,
            "version_page": target.page_url,
            "browser_assisted": True,
        }
        if browser_download_path:
            extra["browser_download_path"] = browser_download_path
        return Artifact(
            self.name,
            request.package,
            target.version,
            url,
            extension,
            normalize_arch(request.arch),
            extra,
        )

    def download(self, artifact: Artifact, destination: Path) -> int:
        browser_path = artifact.extra.get("browser_download_path")
        if browser_path:
            source = Path(browser_path)
            try:
                shutil.copyfile(source, destination)
                return destination.stat().st_size
            finally:
                source.unlink(missing_ok=True)
        return super().download(artifact, destination)

    def resolve_target(self, request: DownloadRequest) -> UptodownTarget:
        """Resolve an exact version and file ID without opening a browser."""
        package = request.package
        app_url, app_id = self._find_app(
            package, request.app_slug, getattr(request, "app_id", None)
        )
        wanted_kind = "xapk" if request.prefer_xapk else "apk"
        if request.version is None:
            entry = self._latest_version(app_url, app_id, wanted_kind)
        else:
            entry = self._find_version(app_url, app_id, str(request.version), wanted_kind)
        version_url = self._version_page_url(app_url, entry)
        file_id = str(entry.get("fileID") or entry.get("versionURL", {}).get("versionID") or "")
        if not file_id:
            raise ProviderError(
                f"Uptodown returned no file ID for {package} {request.version}",
                provider=self.name,
            )
        kind = str(entry.get("kindFile") or entry.get("titleKindFile") or wanted_kind).lower()
        if kind not in {"apk", "xapk"}:
            raise ProviderError(
                f"Uptodown returned unsupported file type {kind!r} for {package}",
                provider=self.name,
            )
        file_id, kind, page_url = self._select_variant(
            request, app_url, app_id, version_url, file_id, kind,
        )
        return UptodownTarget(
            app_url=app_url,
            app_id=app_id,
            version=str(entry.get("version") or request.version),
            file_id=file_id,
            kind=kind,
            page_url=page_url,
            only_xapk="1" if kind == "xapk" else "0",
        )

    def _select_variant(
        self,
        request: DownloadRequest,
        app_url: str,
        app_id: str,
        version_url: str,
        file_id: str,
        kind: str,
    ) -> tuple[str, str, str]:
        """Resolve the ABI variant page for a version.

        The plain ``/download/{file_id}`` page serves Uptodown's own store
        wrapper (package ``com.uptodown``), not the app. The real artifacts
        live behind per-variant ``/download/{file_id}-x`` pages listed in the
        "All variants" panel (``/app/{app_id}/version/{version_id}/files``).
        Without a variants button (single-variant apps) the plain page is
        kept as-is.
        """
        fallback = (file_id, kind, version_url)
        try:
            page = self.http.get(version_url)
        except Exception:
            return fallback
        soup = BeautifulSoup(page.text, "html.parser")
        button = soup.select_one("button.variants[data-version]")
        version_id = (
            str(button.get("data-version") or "").strip() if button else ""
        )
        if not version_id:
            return fallback
        origin = f"{urlparse(app_url).scheme}://{urlparse(app_url).netloc}"
        try:
            response = self.http.get(
                f"{origin}/app/{quote(app_id, safe='')}/version/"
                f"{quote(version_id, safe='')}/files"
            )
        except Exception:
            return fallback
        try:
            payload = response.json()
            panel = payload.get("content", "") if isinstance(payload, dict) else ""
        except Exception:
            panel = response.text
        same = [v for v in _parse_variants(panel) if v["kind"] == kind]
        if not same:
            return fallback
        wanted = normalize_arch(request.arch)

        def satisfies(variant: dict[str, str]) -> bool:
            if wanted in (None, "", "universal", "noarch"):
                return all(
                    matches_abi(variant["abis"], abi) for abi in UNIVERSAL_ABIS
                )
            return matches_abi(variant["abis"], wanted)

        # Keep the entry's own file when it already satisfies the request;
        # otherwise take the first satisfying variant, else the first same-kind
        # one. Never cross container kinds silently.
        ranked = sorted(same, key=lambda v: (v["file_id"] != file_id, not satisfies(v)))
        pick = ranked[0]
        return (
            pick["file_id"],
            pick["kind"],
            f"{app_url.rstrip('/')}/download/{pick['file_id']}-x",
        )

    def _find_app(
        self,
        package: str,
        app_slug: str | None = None,
        app_id: str | None = None,
    ) -> tuple[str, str]:
        candidates: list[str] = []
        hint = str(app_slug or "").strip().strip("/")
        if hint.startswith(("http://", "https://")):
            candidates.append(hint)
        elif hint:
            candidates.extend(
                [
                    f"https://{hint}.en.uptodown.com/android",
                    f"https://{hint}.uptodown.com/android",
                ]
            )

        # A configured numeric ID is authoritative for this provider. This
        # avoids making the exact-version lookup depend on a fragile HTML app
        # page (which can be blocked independently of the versions endpoint).
        if str(app_id or "").strip():
            if not candidates:
                raise ProviderError(
                    f"Uptodown app_id={app_id} requires an app_slug or URL hint",
                    provider=self.name,
                )
            return candidates[0].rstrip("/"), str(app_id).strip()

        candidate_errors: list[str] = []
        for candidate in candidates:
            try:
                page = self.http.get(candidate)
            except Exception as exc:
                candidate_errors.append(f"{candidate}: {exc}")
                continue
            resolved_id = self._app_id(page.text)
            if resolved_id and self._page_matches_package(page.text, package):
                return page.url.rstrip("/"), resolved_id

        if candidates:
            detail = "; ".join(candidate_errors) or "package metadata did not match"
            raise ProviderError(
                f"Uptodown app page lookup failed for {package}: {detail}",
                provider=self.name,
            )

        search_url = f"https://en.uptodown.com/android/search?query={quote(package, safe='')}"
        try:
            search = self.http.get(search_url)
        except Exception as exc:
            raise ProviderError(
                f"Uptodown app search failed for {package}: {exc}", provider=self.name
            ) from exc
        soup = BeautifulSoup(search.text, "html.parser")
        seen: set[str] = set()
        for link in soup.select("a[href]"):
            absolute = urljoin(search.url, link.get("href", "")).split("#", 1)[0]
            if (
                absolute in seen
                or "/android" not in absolute
                or "/search" in absolute
                or ".uptodown.com/" not in absolute
            ):
                continue
            seen.add(absolute)
            try:
                page = self.http.get(absolute)
            except Exception:
                continue
            resolved_id = self._app_id(page.text)
            if resolved_id and self._page_matches_package(page.text, package):
                return page.url.rstrip("/"), resolved_id
        raise ProviderError(f"Uptodown app not found for package {package}", provider=self.name)

    def _latest_version(self, app_url: str, app_id: str, wanted_kind: str) -> dict[str, Any]:
        response = self.http.get(f"{app_url.rstrip('/')}/apps/{app_id}/versions/1")
        try:
            entries = response.json().get("data") or []
        except Exception as exc:
            raise ProviderError(
                f"Uptodown returned invalid latest-version metadata: {exc}",
                provider=self.name,
            ) from exc
        for entry in entries:
            kind = str(entry.get("kindFile") or entry.get("titleKindFile") or "").lower()
            if kind == wanted_kind:
                return entry
        raise ProviderError(
            f"Uptodown has no latest {wanted_kind.upper()} asset for {app_id}",
            provider=self.name,
        )

    def _find_version(
        self, app_url: str, app_id: str, version: str, wanted_kind: str
    ) -> dict[str, Any]:
        target = self._normalize_version(version)
        fallback: dict[str, Any] | None = None
        for page_number in range(1, 21):
            response = self.http.get(
                f"{app_url.rstrip('/')}/apps/{app_id}/versions/{page_number}"
            )
            try:
                payload = response.json()
            except Exception as exc:
                raise ProviderError(
                    f"Uptodown returned invalid version metadata for {app_id}: {exc}",
                    provider=self.name,
                ) from exc
            entries = payload.get("data") or []
            for entry in entries:
                if self._normalize_version(str(entry.get("version", ""))) != target:
                    continue
                kind = str(entry.get("kindFile") or entry.get("titleKindFile") or "").lower()
                if kind == wanted_kind:
                    return entry
                if fallback is None and kind in {"apk", "xapk"}:
                    fallback = entry
            if len(entries) < 100:
                break
        if fallback is not None:
            raise ProviderError(
                f"Uptodown has {version}, but not as a {wanted_kind.upper()} asset",
                provider=self.name,
            )
        raise ProviderError(f"Uptodown version not found: {version}", provider=self.name)

    @staticmethod
    def _version_page_url(app_url: str, entry: dict[str, Any]) -> str:
        parts = entry.get("versionURL") or {}
        values = [
            str(parts.get(key, "")).strip("/")
            for key in ("url", "extraURL", "versionID")
        ]
        if all(values):
            return "/".join(values)
        file_id = entry.get("fileID")
        if file_id:
            return f"{app_url.rstrip('/')}/download/{file_id}"
        raise ProviderError("Uptodown returned no version URL", provider="uptodown")

    @staticmethod
    def _app_id(html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one("#detail-app-name[data-code]")
        return node.get("data-code") if node else None

    @staticmethod
    def _page_matches_package(html: str, package: str) -> bool:
        return package.lower() in html.lower()

    @staticmethod
    def _normalize_version(value: str) -> str:
        return re.sub(r"[\s\-_.]+", "", str(value or "").lower())

    def _cdp_url(self) -> str | None:
        value = str(self.cdp_url or os.getenv("APKD_UPTODOWN_CDP_URL", "")).strip()
        return value or None

    def _manual_click_enabled(self) -> bool:
        if self.manual_click is not None:
            return self.manual_click
        value = os.getenv("APKD_UPTODOWN_MANUAL_CLICK", "").strip().lower()
        return value in {"1", "true", "yes", "on", "manual"}

    def _headless_enabled(self) -> bool:
        if self.headless is not None:
            return self.headless
        value = os.getenv("APKD_UPTODOWN_HEADLESS", "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    @staticmethod
    def _float_setting(value: float | None, env_name: str, default: float,
                       minimum: float, maximum: float) -> float:
        raw = value if value is not None else os.getenv(env_name, "")
        try:
            parsed = float(raw) if raw != "" else default
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _int_setting(value: int | None, env_name: str, default: int,
                     minimum: int, maximum: int) -> int:
        raw = value if value is not None else os.getenv(env_name, "")
        try:
            parsed = int(raw) if raw != "" else default
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def _initial_wait_seconds(self) -> float:
        return self._float_setting(
            self.initial_wait, "APKD_UPTODOWN_INITIAL_WAIT", 5.0, 0.0, 60.0
        )

    def _retry_wait_seconds(self) -> float:
        return self._float_setting(
            self.retry_wait, "APKD_UPTODOWN_RETRY_WAIT", 5.0, 0.0, 60.0
        )

    def _max_attempts(self) -> int:
        return self._int_setting(
            self.max_attempts, "APKD_UPTODOWN_MAX_ATTEMPTS", 2, 1, 3
        )

    def _response_timeout_ms(self) -> int:
        seconds = self._float_setting(
            self.response_timeout,
            "APKD_UPTODOWN_RESPONSE_TIMEOUT",
            30.0,
            5.0,
            180.0,
        )
        return int(seconds * 1000)

    def _browser_enabled(self) -> bool:
        if self.browser is not None:
            return self.browser
        if self._cdp_url():
            return True
        value = os.getenv("APKD_UPTODOWN_BROWSER", "").strip().lower()
        return value in {"1", "true", "yes", "on", "headed"}

    def _invisible_enabled(self) -> bool:
        """Whether to drive the download page with invisible_playwright.

        The stealth engine only presents a coherent browser fingerprint; it
        is not a CAPTCHA solver, and the operator still completes any
        interactive Turnstile manually. An explicit CDP endpoint always wins
        because the invisible engine has no CDP surface.
        """
        if self.invisible is not None:
            return self.invisible
        value = os.getenv("APKD_UPTODOWN_INVISIBLE", "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def _invisible_seed(self) -> int | None:
        if self.invisible_seed is not None:
            return self.invisible_seed
        raw = os.getenv("APKD_UPTODOWN_SEED", "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _browser_download_url(
        self, target: UptodownTarget
    ) -> tuple[str, list[dict[str, Any]], str | None, str, str | None]:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ProviderError(
                "Uptodown browser download requires Playwright; install playwright "
                "and a Chromium/Chrome browser locally",
                provider=self.name,
            ) from exc

        timeout_ms = self._browser_timeout_ms()
        cdp_url = self._cdp_url()
        headless = self._headless_enabled()
        use_invisible = self._invisible_enabled()
        if use_invisible and cdp_url:
            print(
                "Uptodown browser download: a CDP endpoint is set, so the "
                "operator's Chrome wins over invisible mode.",
                file=sys.stderr,
            )
            use_invisible = False
        initial_wait_ms = int(self._initial_wait_seconds() * 1000)
        retry_wait_ms = int(self._retry_wait_seconds() * 1000)
        max_attempts = self._max_attempts()
        response_timeout_ms = min(timeout_ms, self._response_timeout_ms())
        channel = self.channel or os.getenv("APKD_UPTODOWN_BROWSER_CHANNEL") or None
        if cdp_url:
            print(
                "Uptodown browser download: connecting to the operator's Chrome "
                "over CDP; complete the normal Turnstile in that browser.",
                file=sys.stderr,
            )
        elif use_invisible:
            seed = self._invisible_seed()
            print(
                "Uptodown browser download: invisible_playwright stealth engine "
                f"(seed={seed if seed is not None else 'random'}); complete the "
                "normal Turnstile in the browser window if one appears.",
                file=sys.stderr,
            )
        elif headless:
            print(
                "Uptodown browser download: headless mode is active; no human "
                "challenge can be completed automatically.",
                file=sys.stderr,
            )
        else:
            print(
                "Uptodown browser download: complete the Cloudflare Turnstile "
                "in the visible Chromium window when it appears.",
                file=sys.stderr,
            )
        print(
            f"Uptodown click policy: initial_wait={initial_wait_ms / 1000:g}s, "
            f"max_attempts={max_attempts}, retry_wait={retry_wait_ms / 1000:g}s, "
            f"response_timeout={response_timeout_ms / 1000:g}s",
            file=sys.stderr,
        )

        try:
            if use_invisible:
                try:
                    from invisible_playwright import InvisiblePlaywright
                except ImportError as exc:
                    raise ProviderError(
                        "Uptodown invisible mode requires the "
                        "invisible-playwright package; install it and fetch the "
                        "engine (`python -m invisible_playwright fetch`)",
                        provider=self.name,
                    ) from exc
                engine_cm = InvisiblePlaywright(
                    seed=self._invisible_seed(), headless=headless
                )
            else:
                engine_cm = sync_playwright()
            with engine_cm as engine:
                browser = None
                context = None
                page = None
                owns_page = False
                owns_browser = False
                try:
                    if use_invisible:
                        # The engine itself is the browser: a Playwright
                        # Browser with a coherent stealth fingerprint. It is
                        # not a CAPTCHA solver; an interactive Turnstile is
                        # still completed by the operator.
                        browser = engine
                        owns_browser = True
                        context = (
                            browser.contexts[0]
                            if browser.contexts
                            else browser.new_context(locale="en-US")
                        )
                        page = context.new_page()
                        downloads: list[Any] = []

                        def on_download(download: Any) -> None:
                            downloads.append(download)

                        page.on("download", on_download)
                        owns_page = True
                    elif cdp_url:
                        browser = engine.chromium.connect_over_cdp(
                            cdp_url, timeout=timeout_ms
                        )
                        if not browser.contexts:
                            raise ProviderError(
                                "The CDP browser has no usable browser context; "
                                "start Chrome with a dedicated --user-data-dir",
                                provider=self.name,
                            )
                        context = browser.contexts[0]
                        page = context.new_page()
                        downloads: list[Any] = []

                        def on_download(download: Any) -> None:
                            downloads.append(download)

                        page.on("download", on_download)
                        owns_page = True
                    else:
                        launch_kwargs: dict[str, Any] = {"headless": headless}
                        if channel:
                            launch_kwargs["channel"] = channel
                        if headless:
                            launch_kwargs["args"] = ["--no-sandbox"]
                        browser = engine.chromium.launch(**launch_kwargs)
                        owns_browser = True
                        context = browser.new_context(locale="en-US")
                        page = context.new_page()
                        downloads = []

                        def on_download(download: Any) -> None:
                            downloads.append(download)

                        page.on("download", on_download)

                    page.bring_to_front()
                    page.goto(
                        target.page_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    user_agent = page.evaluate("() => navigator.userAgent")
                    try:
                        webdriver = page.evaluate("() => navigator.webdriver")
                    except Exception:
                        webdriver = "unknown"
                    print(
                        f"Uptodown browser: {user_agent} "
                        f"(navigator.webdriver={webdriver})",
                        file=sys.stderr,
                    )
                    button = page.locator("#detail-download-button")
                    button.wait_for(state="visible", timeout=timeout_ms)
                    button.scroll_into_view_if_needed(timeout=timeout_ms)
                    self._validate_button_metadata(button, target)
                    if not button.get_attribute("data-url"):
                        try:
                            page.wait_for_function(
                                "() => typeof window.turnstile !== 'undefined'",
                                timeout=min(timeout_ms, 30000),
                            )
                        except Exception as exc:
                            raise ProviderError(
                                "Uptodown's Turnstile API did not become ready; "
                                "check that the normal browser can load "
                                "challenges.cloudflare.com",
                                provider=self.name,
                            ) from exc
                    try:
                        page.wait_for_function(
                            """() => {
                                const button = document.querySelector('#detail-download-button');
                                if (!button || button.disabled) return false;
                                const style = window.getComputedStyle(button);
                                return style.pointerEvents !== 'none' &&
                                    style.display !== 'none' &&
                                    style.visibility !== 'hidden';
                            }""",
                            timeout=min(timeout_ms, 30000),
                        )
                    except Exception as exc:
                        raise ProviderError(
                            "Uptodown's Download button did not become actionable",
                            provider=self.name,
                        ) from exc

                    def is_download_response(response: Any) -> bool:
                        return (
                            response.request.method == "POST"
                            and "/ajax/app/" in response.url
                            and "/download-url" in response.url
                        )

                    if self._manual_click_enabled():
                        with page.expect_response(
                            is_download_response,
                            timeout=timeout_ms,
                        ) as response_info:
                            print(
                                "Click Uptodown's Download button yourself in "
                                "the visible browser, complete the normal "
                                "Turnstile, then return here and press Enter.",
                                file=sys.stderr,
                            )
                            input("Press Enter after the browser response is ready: ")
                        response = response_info.value
                    else:
                        if initial_wait_ms:
                            page.wait_for_timeout(initial_wait_ms)
                        response = None
                        for attempt in range(1, max_attempts + 1):
                            if attempt > 1:
                                print(
                                    f"Uptodown: no download response on attempt "
                                    f"{attempt - 1}; retrying after "
                                    f"{retry_wait_ms / 1000:g}s.",
                                    file=sys.stderr,
                                )
                                if retry_wait_ms:
                                    page.wait_for_timeout(retry_wait_ms)
                            try:
                                with page.expect_response(
                                    is_download_response,
                                    timeout=response_timeout_ms,
                                ) as response_info:
                                    button.click()
                                response = response_info.value
                                break
                            except PlaywrightTimeoutError as exc:
                                if attempt >= max_attempts:
                                    raise ProviderError(
                                        "Uptodown did not return a download URL "
                                        f"response after {max_attempts} normal "
                                        "click attempt(s); headless mode cannot "
                                        "complete an interactive Turnstile",
                                        provider=self.name,
                                    ) from exc
                    payload, response_text = self._read_response(response)
                    if response.status != 200:
                        raise ProviderError(
                            self._http_error_message(
                                response.status, response.url, payload, response_text
                            ),
                            provider=self.name,
                        )
                    if payload is None:
                        raise ProviderError(
                            "Uptodown returned a non-JSON download response",
                            provider=self.name,
                        )
                    url = self._download_url_from_payload(payload)
                    cookies = context.cookies()
                    browser_download_path = self._capture_browser_download(
                        page, downloads, target
                    )
                    return url, cookies, user_agent, page.url, browser_download_path
                finally:
                    if owns_page and page is not None:
                        try:
                            page.close()
                        except Exception:
                            pass
                    if owns_browser and browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            pass
        except ProviderError:
            raise
        except Exception as exc:
            cdp_hint = (
                " Use --cdp-url with a normal Chrome if Turnstile rejects "
                "Playwright's bundled Chromium."
                if not cdp_url
                else " Check that the operator's Chrome is still running and "
                "the CDP endpoint is reachable."
            )
            if use_invisible:
                cdp_hint += (
                    " Invisible mode only presents a coherent fingerprint; a "
                    "persistent interactive challenge still needs an operator, "
                    "and another APKD_UPTODOWN_SEED sometimes passes."
                )
            raise ProviderError(
                "Uptodown browser download did not complete; solve the normal "
                f"Turnstile challenge in the visible browser and retry "
                f"({type(exc).__name__}: {exc}).{cdp_hint}",
                provider=self.name,
            ) from exc

    @staticmethod
    def _capture_browser_download(
        page: Any,
        downloads: list[Any],
        target: UptodownTarget,
    ) -> str | None:
        if not downloads:
            try:
                page.wait_for_event("download", timeout=5000)
            except Exception:
                return None
        if not downloads:
            return None
        suffix = ".xapk" if target.kind == "xapk" else ".apk"
        temporary = tempfile.NamedTemporaryFile(
            prefix="uptodown-browser-",
            suffix=suffix,
            delete=False,
        )
        temporary_path = Path(temporary.name)
        temporary.close()
        try:
            downloads[0].save_as(str(temporary_path))
        except Exception:
            temporary_path.unlink(missing_ok=True)
            return None
        return str(temporary_path)

    @staticmethod
    def _validate_button_metadata(button: Any, target: UptodownTarget) -> None:
        app_id = str(button.get_attribute("data-app-id") or "").strip()
        file_id = str(button.get_attribute("data-file-id") or "").strip()
        if not app_id or not file_id:
            raise ProviderError(
                "Uptodown download button is missing app/file metadata",
                provider="uptodown",
            )
        if app_id != str(target.app_id) or file_id != str(target.file_id):
            raise ProviderError(
                "Uptodown download button metadata does not match the resolved "
                f"target (expected app_id={target.app_id}, file_id={target.file_id}; "
                f"got app_id={app_id}, file_id={file_id})",
                provider="uptodown",
            )

    @staticmethod
    def _read_response(response: Any) -> tuple[Any, str]:
        try:
            return response.json(), ""
        except Exception:
            try:
                text = str(response.text() or "")
            except Exception:
                text = ""
            try:
                return json.loads(text), text
            except Exception:
                return None, text

    @staticmethod
    def _redact_response_text(text: str) -> str:
        """Strip bearer-ish values before a response body reaches an exception.

        Uptodown's AJAX payload carries the CDN grant under ``downloadURL``,
        not ``token``, and a failing lookup would otherwise print a live
        download URL into stderr and CI logs.
        """
        redacted = re.sub(
            r"(?i)((?:download_?url|token|k|session_?id)[^'\"]*['\"]?\s*[:=]\s*['\"])"
            r"[^'\"\s,}&]+",
            r"\1<redacted>",
            str(text or ""),
        )
        return redacted[:300]

    @classmethod
    def _http_error_message(
        cls,
        status: int,
        url: str,
        payload: Any,
        response_text: str,
    ) -> str:
        endpoint = url.split("?", 1)[0]
        details: list[str] = [f"HTTP {status}", f"endpoint={endpoint}"]
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                for key in ("success", "errorCode", "errorMsg", "error", "message"):
                    if key in data and data[key] not in (None, ""):
                        details.append(f"{key}={data[key]}")
            for key in ("success", "errorCode", "errorMsg", "error", "message"):
                if key in payload and payload[key] not in (None, ""):
                    details.append(f"{key}={payload[key]}")
        elif response_text:
            details.append(f"body={cls._redact_response_text(response_text)!r}")
        return "Uptodown download URL request failed: " + ", ".join(details)

    @staticmethod
    def _download_url_from_payload(payload: Any) -> str:
        data = payload.get("data") if isinstance(payload, dict) else None
        url = data.get("downloadURL") if isinstance(data, dict) else None
        if not url:
            detail = ""
            if isinstance(payload, dict):
                candidate = payload.get("errorMsg") or payload.get("error")
                if isinstance(data, dict):
                    candidate = candidate or data.get("errorMsg") or data.get("error")
                if candidate:
                    detail = f"; server said: {candidate}"
            raise ProviderError(
                "Uptodown did not return a download URL after browser verification"
                + detail,
                provider="uptodown",
            )
        return UptodownProvider._normalise_download_url(str(url))

    @staticmethod
    def _normalise_download_url(value: str) -> str:
        url = str(value or "").strip()
        if not url:
            raise ProviderError(
                "Uptodown returned an empty download URL",
                provider="uptodown",
            )
        if url.startswith("//"):
            return f"https:{url}"
        if url.startswith(("http://", "https://")):
            return url
        # The website's AJAX response normally contains a CDN token, not a
        # complete URL; its own JavaScript prefixes it with this host.
        if url.startswith("dw.uptodown.com/"):
            return f"https://{url}"
        if url.startswith("dwn/"):
            return f"https://dw.uptodown.com/{url}"
        return f"https://dw.uptodown.com/dwn/{url.lstrip('/')}"

    @staticmethod
    def _browser_timeout_ms() -> int:
        try:
            seconds = float(os.getenv("APKD_UPTODOWN_BROWSER_TIMEOUT", "180"))
        except ValueError:
            seconds = 180.0
        return max(30, int(seconds * 1000))

    def _store_cookies(self, cookies: list[dict[str, Any]]) -> None:
        for cookie in cookies:
            try:
                self.http.session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/"),
                )
            except Exception:
                continue
