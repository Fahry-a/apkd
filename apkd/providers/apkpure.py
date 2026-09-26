from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse

from bs4 import BeautifulSoup

from ._apkpure import client as _client_mod
from ._apkpure.config import Config
from ._apkpure.transport import supported_impersonation
from .base import ABI_SPELLINGS, UNIVERSAL_ABIS, Provider, matches_abi, normalize_arch
from ..models import Artifact, DownloadRequest, ProviderError

try:
    from curl_cffi import requests as _web_requests
except ImportError:  # pragma: no cover - curl_cffi is a project dependency
    _web_requests = None

# apkpure.com rate-limits by TLS fingerprint, not by address: Android Chrome
# fingerprints are served, desktop ones are answered with 403. Only targets the
# installed curl_cffi actually accepts are attempted.
_WEB_IMPERSONATE = supported_impersonation(
    ("chrome131_android", "chrome_android", "chrome131")
)


def _covers_universal(context: str) -> bool:
    """True when an asset advertises both ARM ABIs."""
    return all(matches_abi(context, abi) for abi in UNIVERSAL_ABIS)


def _mentions_any_abi(context: str) -> bool:
    """True when an asset advertises at least one known ABI."""
    return any(matches_abi(context, abi) for abi in ABI_SPELLINGS)


class APKPureProvider(Provider):
    name = "apkpure"
    capabilities = {"version": True, "arch": False, "dpi": False, "min_sdk": False}

    def __init__(self, http=None, client_module: Any | None = None, config: Any | None = None) -> None:
        super().__init__(http)
        self._client_module = client_module or _client_mod
        self._config = config

    def _client(self):
        return self._client_module, self._config or Config()

    def resolve_request(self, request: DownloadRequest) -> Artifact:
        client_mod, cfg = self._client()
        try:
            if request.version is None:
                resp = client_mod.app_detail(cfg, request.package, use_cache=False)
                info = client_mod.extract_asset(resp)
            else:
                info = self._find_version(client_mod, cfg, request)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"APKPure lookup failed for {request.package}: {exc}",
                                provider=self.name) from exc
        url = (info or {}).get("asset_url")
        version = str((info or {}).get("version_name") or request.version or "")
        if not url:
            raise ProviderError(f"APKPure returned no asset URL for {request.package} {version}",
                                provider=self.name)
        asset_type = str((info or {}).get("asset_type") or "").upper()
        extension = {
            "XAPK": ".xapk",
            "APKM": ".apkm",
            "APKS": ".apks",
        }.get(asset_type, ".apk")
        if extension == ".apk" and request.prefer_xapk:
            extension = ".xapk"
        arch = normalize_arch(request.arch)
        extra = {}
        if (info or {}).get("file_sha256"):
            extra["sha256"] = info["file_sha256"]
        return Artifact(self.name, request.package, version, url, extension, arch, extra)

    def _find_version(self, client_mod: Any, cfg: Any, request: DownloadRequest) -> dict:
        package = request.package
        version = str(request.version)
        try:
            hist = client_mod.app_his_version(cfg, package)
        except Exception as exc:
            raise ProviderError(f"APKPure version history failed for {package}: {exc}",
                                provider=self.name) from exc
        found = self._search_history(hist, version)
        if found is not None:
            # Entries from history may already be detail-shaped or asset-shaped.
            if isinstance(found, dict) and found.get("asset_url"):
                return found
            try:
                extracted = client_mod.extract_asset(found)
            except Exception:
                extracted = None
            if extracted and extracted.get("asset_url"):
                return extracted
            asset = (found.get("asset") or {}) if isinstance(found, dict) else {}
            url = asset.get("url") or found.get("url")
            if url:
                return {
                    "asset_url": url,
                    "version_name": found.get("version_name") or asset.get("vername") or version,
                    "asset_type": asset.get("type", ""),
                }
        # Fallback to latest detail and verify it matches the requested version.
        resp = client_mod.app_detail(cfg, package, use_cache=False)
        info = client_mod.extract_asset(resp)
        if str(info.get("version_name") or "") == version:
            return info

        # The signed history endpoint occasionally returns an empty catalog
        # even though the public version page still exposes the exact release.
        # Use that page only as a fallback; it is still exact-version checked by
        # the caller and never silently substitutes ``latest``.
        web_info = self._find_web_version(request)
        if web_info:
            return web_info
        raise ProviderError(f"APKPure version not found: {package} {version}", provider=self.name)

    def _find_web_version(self, request: DownloadRequest) -> dict | None:
        """Resolve an exact old release from APKPure's public version pages.

        This is the fallback for the signed API's empty history response.  Only
        metadata pages are fetched here; the returned CDN URL is streamed later
        by ``Provider.download``.
        """
        if _web_requests is None:
            return None
        version = str(request.version)
        requested_arch = normalize_arch(request.arch)
        arch_blocked = False
        for base in self._web_bases(request):
            for detail_url in self._web_detail_urls(base, request.package, version):
                try:
                    response = self._web_get(detail_url)
                except Exception:  # noqa: BLE001 - try the next candidate page
                    continue
                if response is None or response.status_code != 200:
                    continue
                if "Just a moment" in response.text:
                    continue
                candidates = self._web_asset_candidates(response.text, str(response.url))
                if not candidates:
                    continue
                selection = self._select_asset(candidates, request)
                if selection is None:
                    arch_blocked = True
                    continue
                kind, url = selection
                return {
                    "asset_url": url,
                    "version_name": version,
                    "asset_type": kind,
                    "source": "web-version-page",
                }
        if arch_blocked:
            if requested_arch == "universal":
                raise ProviderError(
                    f"APKPure has exact version {version}, but only separate "
                    "arm64-v8a/armeabi-v7a assets; no universal package is published",
                    provider=self.name,
                )
            raise ProviderError(
                f"APKPure has exact version {version}, but publishes no asset "
                f"for arch={requested_arch}",
                provider=self.name,
            )
        return None

    def _web_bases(self, request: DownloadRequest) -> list[str]:
        """Candidate ``https://apkpure.com/{slug}/{package}`` roots for an app.

        APKPure redirects any slug segment to the canonical one, so a probe
        under a throwaway slug resolves the real slug for us.  That avoids
        depending on the search page, which renders its results client-side and
        therefore exposes no result links to scrape.
        """
        package = quote(request.package, safe="-._")
        bases: list[str] = []

        def add_base(value: str | None) -> None:
            if not value:
                return
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or "apkpure.com" not in parsed.netloc:
                return
            base = value.rstrip("/")
            if base not in bases:
                bases.append(base)

        hint = str(request.app_slug or "").strip().strip("/")
        if hint:
            for slug in (hint, f"{hint}-app"):
                add_base(f"https://apkpure.com/{quote(slug, safe='-._')}/{package}")

        # The detail page (unlike /versions) is reachable for every app and
        # answers with a redirect to the canonical slug.
        for probe in (f"https://apkpure.com/_/{package}",):
            try:
                response = self._web_get(probe)
            except Exception:  # noqa: BLE001 - fall through to the next probe
                response = None
            if response is None or response.status_code != 200:
                continue
            resolved = str(response.url)
            if request.package in urlparse(resolved).path:
                add_base(resolved)

        return bases

    def _web_detail_urls(self, base: str, package: str, version: str) -> list[str]:
        """Detail pages that may carry ``version``, canonical listing first.

        The direct ``/download/{version}`` URL is the common case.  The
        ``/versions`` index is consulted as well because a release can be
        listed under a normalised slug (1.4.1 -> 1.4) that the direct guess
        does not match.
        """
        quoted_package = quote(package, safe="-._")
        direct = f"{base}/download/{quote(version, safe='-._')}"
        urls = [direct]
        try:
            listing = self._web_get(f"{base}/versions")
        except Exception:  # noqa: BLE001 - the direct URL may still work
            listing = None
        if listing is None or listing.status_code != 200:
            return urls
        try:
            response = self._web_get(direct)
        except Exception:  # noqa: BLE001 - listing already consulted
            response = None
        if response is not None and response.status_code == 200:
            for candidate in self._web_asset_candidates(response.text, str(response.url)):
                urls.append(candidate[1])
                break
        for match in re.finditer(
            rf'href="([^"]*/{re.escape(quoted_package)}/download/[^"]+)"', listing.text
        ):
            href = urljoin(str(listing.url), match.group(1))
            if href not in urls and href.rsplit("/", 1)[-1] == quote(version, safe="-._"):
                urls.insert(0, href)
        return urls

    @staticmethod
    def _select_asset(
        candidates: list[tuple[str, str, str]], request: DownloadRequest
    ) -> tuple[str, str] | None:
        """Pick the asset honouring the requested container type and ABI.

        Returns ``None`` only when the page really does publish ABI-specific
        assets and none of them matches the request. A page whose assets
        advertise no ABI at all is architecture-independent and satisfies any
        request.
        """
        wanted_types = {"XAPK", "APKM", "APKS"} if request.prefer_xapk else {"APK"}
        typed = [candidate for candidate in candidates if candidate[0] in wanted_types]
        if not typed and not request.prefer_xapk:
            # A provider may expose only a bundle for a nominally APK request.
            # Return it honestly; the caller's file_type check decides whether
            # that is acceptable.
            typed = candidates
        if not typed:
            return None

        requested_arch = normalize_arch(request.arch)
        arch_specific = [c for c in typed if _mentions_any_abi(c[2])]
        if not arch_specific or not requested_arch or requested_arch == "noarch":
            return typed[0][0], typed[0][1]
        if requested_arch == "universal":
            universal = [c for c in arch_specific if _covers_universal(c[2])]
            if not universal:
                return None
            return universal[0][0], universal[0][1]
        for candidate in arch_specific:
            if matches_abi(candidate[2], requested_arch):
                return candidate[0], candidate[1]
        # A request for a concrete ABI that no asset advertises is unsatisfiable
        # here, but the release may simply have no native code at all. Do not
        # claim a mismatch we cannot substantiate.
        return None

    @staticmethod
    def _web_get(url: str):
        """GET a public page with a small Android/desktop impersonation chain."""
        last = None
        for impersonate in _WEB_IMPERSONATE:
            try:
                response = _web_requests.get(
                    url,
                    impersonate=impersonate,
                    timeout=30,
                    allow_redirects=True,
                )
                last = response
                if response.status_code == 200:
                    return response
            except Exception as exc:  # noqa: BLE001 - try next transport
                last = exc
        if isinstance(last, Exception):
            return None
        return last

    @staticmethod
    def _web_asset_candidates(html: str, page_url: str) -> list[tuple[str, str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        candidates: list[tuple[str, str, str]] = []
        for link in soup.select("a[href]"):
            href = urljoin(page_url, link.get("href", ""))
            parsed = urlparse(href)
            if "d.apkpure.com" not in parsed.netloc:
                continue
            match = re.search(r"/b/(APK|XAPK|APKM|APKS)(?:/|\?)", parsed.path, re.I)
            if not match:
                continue
            kind = match.group(1).upper()
            container = link.find_parent(["li", "div", "section"]) or link
            query = parse_qs(parsed.query)
            native_code = " ".join(query.get("nc", []))
            context = (
                f"{link.get_text(' ', strip=True)} "
                f"{container.get_text(' ', strip=True)} {native_code}"
            )
            candidates.append((kind, href, context))
        return candidates

    # Bound the walk: this descends an arbitrary JSON response, and a deeply
    # nested or self-referential one would otherwise recurse until the
    # interpreter gives up rather than raising a reportable error.
    _MAX_HISTORY_DEPTH = 32

    @classmethod
    def _search_history(cls, node: Any, version: str, depth: int = 0) -> Any | None:
        if depth > cls._MAX_HISTORY_DEPTH:
            return None
        if isinstance(node, dict):
            if str(node.get("version_name") or "") == version:
                return node
            for value in node.values():
                hit = cls._search_history(value, version, depth + 1)
                if hit is not None:
                    return hit
        elif isinstance(node, list):
            for item in node:
                hit = cls._search_history(item, version, depth + 1)
                if hit is not None:
                    return hit
        return None
