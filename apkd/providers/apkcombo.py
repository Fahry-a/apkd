from __future__ import annotations

import json
import re
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .base import Provider
from ..models import Artifact, ProviderError


class APKComboProvider(Provider):
    name = "apkcombo"

    def resolve(self, package: str, version: str | None = None, arch: str | None = None) -> Artifact:
        app_url = self._find_app(package)
        page = self.http.get(app_url)
        soup = BeautifulSoup(page.text, "html.parser")
        current = self._schema_version(soup)

        if version is None:
            version = current
        if not version:
            raise ProviderError(f"APKCombo did not expose a version for {package}")

        if self._same_version(current, version):
            download_url = self._download_url(soup, page.url)
            return Artifact(self.name, package, version, download_url, self._extension(soup, download_url), arch)

        old = self.http.get(app_url.rstrip("/") + "/old-versions/")
        old_soup = BeautifulSoup(old.text, "html.parser")
        target = self._find_version_link(old_soup, version)
        if not target:
            raise ProviderError(f"APKCombo version not found: {version}")

        version_page = self.http.get(urljoin(old.url, target))
        version_soup = BeautifulSoup(version_page.text, "html.parser")
        actual = self._schema_version(version_soup) or version
        if not self._same_version(actual, version):
            raise ProviderError(f"APKCombo returned version {actual}, requested {version}")

        download_url = self._download_url(version_soup, version_page.url)
        return Artifact(
            self.name,
            package,
            version,
            download_url,
            self._extension(version_soup, download_url),
            arch,
        )

    def _find_app(self, package: str) -> str:
        # /app/<package>/download redirects to the canonical slug/package page.
        response = self.http.get(
            f"https://apkcombo.com/app/{quote(package, safe='')}/download"
        )
        final_url = response.url.rstrip("/")
        parsed = urlparse(final_url)
        if parsed.netloc.endswith("apkcombo.com") and f"/{package}" in parsed.path:
            return final_url

        search = self.http.get(
            f"https://apkcombo.com/search?q={quote(package, safe='')}"
        )
        soup = BeautifulSoup(search.text, "html.parser")
        for link in soup.select("a[href]"):
            absolute = urljoin(search.url, link["href"])
            parsed = urlparse(absolute)
            if parsed.netloc.endswith("apkcombo.com") and f"/{package}" in parsed.path:
                return absolute.rstrip("/")

        raise ProviderError(f"APKCombo app not found for package {package}")

    @staticmethod
    def _schema_version(soup: BeautifulSoup) -> str | None:
        for script in soup.select("script[type='application/ld+json']"):
            try:
                data = json.loads(script.string or script.get_text())
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict) and data.get("softwareVersion"):
                return str(data["softwareVersion"])

        node = soup.select_one("[itemprop='softwareVersion']")
        return node.get_text(" ", strip=True) if node else None

    def _find_version_link(self, soup: BeautifulSoup, version: str) -> str | None:
        for link in soup.select("a[href]"):
            text = link.get_text(" ", strip=True)
            if self._same_version(text, version):
                return link.get("href")

        for node in soup.select("[data-version][href], a[href]"):
            value = node.get("data-version")
            if value and self._same_version(value, version):
                return node.get("href")

        return None

    def _download_url(self, soup: BeautifulSoup, page_url: str) -> str:
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            classes = " ".join(link.get("class") or [])
            if "/r2?u=" in href:
                return self._clean_download_link(urljoin(page_url, href))
            if "variant" in classes.lower() and (
                ".apk" in href.lower() or ".xapk" in href.lower() or "/download" in href.lower()
            ):
                return urljoin(page_url, href)
            if "/download" in href.lower() and (
                ".apk" in href.lower() or ".xapk" in href.lower() or "download" in link.get_text(" ", strip=True).lower()
            ):
                return urljoin(page_url, href)

        raise ProviderError("APKCombo did not expose a direct download link")

    @staticmethod
    def _clean_download_link(link: str) -> str:
        parsed = urlparse(link)
        if parsed.path == "/r2" and "u" in parsed.query:
            return unquote(parsed.query.split("u=", 1)[1])
        return link

    @staticmethod
    def _extension(soup: BeautifulSoup, url: str) -> str:
        text = soup.get_text(" ", strip=True).lower()
        if ".xapk" in text or ".xapk" in url.lower():
            return ".xapk"
        if ".apkm" in text or ".apkm" in url.lower():
            return ".apkm"
        if ".apks" in text or ".apks" in url.lower():
            return ".apks"
        return ".apk"

    @staticmethod
    def _same_version(left: str | None, right: str | None) -> bool:
        if not left or not right:
            return False
        clean = lambda value: re.sub(r"[^a-z0-9.]+", "", value.lower())
        return clean(left) == clean(right)
