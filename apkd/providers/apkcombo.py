from __future__ import annotations

import re
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup

from .base import Provider, abi_spellings, matches_abi, normalize_arch
from ..models import Artifact, DownloadRequest, ProviderError


def _text_contains_version(text: str | None, version: str | None) -> bool:
    """Check whether a link label mentions an exact version.

    Old-versions link labels carry app name, bundle type and date
    (``Native Camera 1.4.1 XAPK Sep 10, 2026``), so whole-label equality
    never matches. Match the version as a standalone token instead, with
    digit/dot boundaries so ``1.4.1`` does not match ``1.4.10``.
    """
    if not text or not version:
        return False
    label = re.sub(r"[\s\-_]+", " ", text).strip().lower()
    want = re.sub(r"[\s\-_]+", " ", version).strip().lower()
    return re.search(r"(?<![\d.])" + re.escape(want) + r"(?![\d.])", label) is not None


def _url_contains_version(url: str | None, version: str | None) -> bool:
    """Match a version in an APKCombo version URL.

    APKCombo uses both dotted versions and separator-normalised paths, for
    example ``1.4.1`` and ``1-4-1``.  A boundary after the final component is
    important: ``1.4`` must not match the unrelated ``1.4.2`` page.
    """
    if not url or not version:
        return False
    value = str(version).strip().lower()
    if not value:
        return False
    pattern = r"(?<![0-9])" + r"[-_.]?".join(
        re.escape(part) for part in re.split(r"[-_.]+", value)
    ) + r"(?![0-9]|[-_.][0-9])"
    return re.search(pattern, str(url).lower()) is not None


class APKComboProvider(Provider):
    name = "apkcombo"
    capabilities = {"version": True, "arch": False, "dpi": False, "min_sdk": False}

    def resolve_request(self, request: DownloadRequest) -> Artifact:
        package = request.package
        version = request.version
        arch = normalize_arch(request.arch)
        self.http.timeout = request.timeout
        app_url = self._find_app(package)
        page = self.http.get(app_url)
        soup = BeautifulSoup(page.text, "html.parser")
        current = self._schema_version(soup)
        if version is None:
            version = current
        if self._same_version(current, version):
            download_url = self._download_url(soup, app_url, arch)
            extension = self._extension(soup, download_url, request.prefer_xapk)
            return Artifact(self.name, package, version or "", download_url, extension, arch)

        old = self.http.get(app_url.rstrip("/") + "/old-versions/")
        old_soup = BeautifulSoup(old.text, "html.parser")
        target = self._find_version_link(old_soup, version or "")
        if not target:
            raise ProviderError(f"APKCombo version not found: {version}", provider=self.name)
        version_page = self.http.get(urljoin(old.url, target))
        version_soup = BeautifulSoup(version_page.text, "html.parser")
        download_url = self._download_url(
            version_soup, version_page.url, arch, expected_version=version
        )
        extension = self._extension(version_soup, download_url, request.prefer_xapk)
        return Artifact(self.name, package, version or "", download_url, extension, arch)

    def _find_app(self, package: str) -> str:
        # APKCombo's canonical application URL contains both a slug and package.
        search = self.http.get(f"https://apkcombo.com/search?q={quote(package, safe='')}")
        # An exact search often resolves straight to the app page.
        if f"/{package}/" in search.url:
            return search.url.rstrip("/")
        soup = BeautifulSoup(search.text, "html.parser")
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            absolute = urljoin(search.url, href)
            if f"/{package}/" in absolute and "apkcombo.com" in absolute:
                return absolute.rstrip("/")
        raise ProviderError(f"APKCombo app not found for package {package}", provider=self.name)

    def _schema_version(self, soup: BeautifulSoup) -> str | None:
        for script in soup.select("script[type='application/ld+json']"):
            try:
                import json
                data = json.loads(script.string or script.get_text())
            except Exception:
                continue
            if isinstance(data, dict) and data.get("softwareVersion"):
                return str(data["softwareVersion"])
        node = soup.select_one("[itemprop='softwareVersion']")
        return node.get_text(strip=True) if node else None

    def _find_version_link(self, soup: BeautifulSoup, version: str) -> str | None:
        for link in soup.select("a[href]"):
            text = link.get_text(" ", strip=True)
            href = link.get("href", "")
            if _text_contains_version(text, version):
                return href
        # Search embedded structured data as a fallback.
        for script in soup.select("script"):
            if version in script.get_text():
                match = re.search(r'https?://[^"\\s]+', script.get_text())
                if match:
                    return match.group(0)
        return None

    def _download_url(
        self,
        soup: BeautifulSoup,
        page_url: str,
        arch: str | None,
        *,
        expected_version: str | None = None,
    ) -> str:
        candidates = []
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            text = link.get_text(" ", strip=True).lower()
            if "/download/" in href and ("apk" in text or "apk" in href):
                url = urljoin(page_url, href)
                if url not in candidates:
                    candidates.append(url)
        if not candidates:
            install = soup.find("link", rel="alternate")
            if install and install.get("href"):
                candidates.append(urljoin(page_url, install["href"]))
        if not candidates:
            raise ProviderError("APKCombo did not expose a download link", provider=self.name)
        # Download links open an interstitial page; the real file is exposed
        # through ``a.variant`` links. Current APKCombo uses both
        # ``/r2?u=<url>&...`` and ``/d?u=<opaque>`` forms. Keep the complete
        # redirect URL because its extra parameters are part of the download
        # contract.
        errors = []
        for candidate in candidates:
            try:
                return self._resolve_variant_url(
                    candidate, arch, expected_version=expected_version
                )
            except ProviderError as exc:
                errors.append(f"{candidate}: {exc}")
                continue
        raise ProviderError(
            f"APKCombo could not resolve a file URL ({'; '.join(errors)})",
            provider=self.name,
        )

    @staticmethod
    def _direct_variant_url(href: str, base_url: str) -> str | None:
        """Return APKCombo's complete redirect URL, including guard params.

        The ``/r2?u=...`` endpoint may carry ``fp``, ``ip``, package and
        language parameters in addition to the signed target.  Decoding only
        ``u`` drops those parameters and can make an otherwise valid download
        fail.  Returning the complete endpoint lets the HTTP client follow the
        redirect while preserving the provider's requested variant exactly.
        """
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.path.rstrip("/") not in {"/r2", "/d"}:
            return None
        return absolute

    def _resolve_variant_url(
        self,
        page_url: str,
        arch: str | None,
        *,
        expected_version: str | None = None,
    ) -> str:
        response = self.http.get(page_url)
        if expected_version and not _url_contains_version(
            str(response.url or ""), expected_version
        ):
            raise ProviderError(
                f"download page is for a different version than {expected_version}",
                provider=self.name,
            )
        content_type = response.headers.get("content-type", "")
        if "vnd.android.package-archive" in content_type or "octet-stream" in content_type:
            return response.url
        soup = BeautifulSoup(response.text, "html.parser")
        variants = []
        for link in soup.select("a.variant[href], a[href*='?u=']"):
            href = link.get("href", "")
            direct = self._direct_variant_url(href, response.url)
            if not direct:
                continue
            context_parts = []
            node = link
            for _ in range(4):
                if node is None:
                    break
                text = node.get_text(" ", strip=True).lower()
                if text:
                    context_parts.append(text)
                code_text = " ".join(
                    code.get_text(" ", strip=True).lower()
                    for code in node.find_all("code")
                )
                if any(matches_abi(code_text, abi) for abi in ("arm64-v8a", "armeabi-v7a")):
                    # The nearest explicit ABI container is authoritative;
                    # do not absorb sibling variants from an outer list.
                    context_parts.append(code_text)
                    break
                node = node.parent
            context = " ".join([href, direct, *context_parts])
            variants.append((direct, context))
        if not variants:
            raise ProviderError("no variant file link on download page")
        wanted = normalize_arch(arch)
        if wanted and wanted != "noarch":
            if wanted == "universal":
                # A context advertising no ARM ABI at all is
                # architecture-independent, which satisfies universal. One that
                # names only arm64 (or only arm32) does not, and is never
                # relabelled as universal.
                arm64 = abi_spellings("arm64-v8a")
                arm32 = abi_spellings("armeabi-v7a")
                any_arm = abi_spellings("universal")
                universal = [
                    url for url, context in variants
                    if "universal" in context
                    or (
                        any(alias in context for alias in arm64)
                        and any(alias in context for alias in arm32)
                    )
                    or not any(alias in context for alias in any_arm)
                ]
                if not universal:
                    raise ProviderError(
                        "universal requires an APK/XAPK asset containing both "
                        "arm64-v8a and armeabi-v7a; this page only exposes "
                        "architecture-specific variants",
                        provider=self.name,
                    )
                return universal[0]
            # Prefer a URL that names the ABI outright, then fall back to the
            # surrounding markup.
            for url, _ in variants:
                if any(alias in url.lower() for alias in abi_spellings(wanted)):
                    return url
            for url, context in variants:
                if matches_abi(context, wanted):
                    return url
        return variants[0][0]

    @staticmethod
    def _extension(soup: BeautifulSoup, url: str, prefer_xapk: bool = False) -> str:
        """Infer the actual container without guessing from the page URL alone."""
        haystack = f"{url} {soup.get_text(' ', strip=True)}".lower()
        for extension in (".apkm", ".apks", ".xapk", ".apk"):
            if extension in haystack:
                return extension
        if prefer_xapk:
            return ".xapk"
        return ".apk"

    @staticmethod
    def _same_version(left: str | None, right: str | None) -> bool:
        if not left or not right:
            return False

        def clean(v: str) -> str:
            v = re.sub(r"\s*[\[\(].*?[\]\)]", "", v).strip().lower()
            return re.sub(r"[\s\-_]+", "", v)

        return clean(left) == clean(right)
