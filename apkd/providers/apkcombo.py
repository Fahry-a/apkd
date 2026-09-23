from __future__ import annotations

import re
from urllib.parse import quote, urljoin

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
        if self._same_version(current, version):
            download_url = self._download_url(soup, app_url, arch)
            return Artifact(self.name, package, version, download_url, ".apk", arch)

        old = self.http.get(app_url.rstrip("/") + "/old-versions/")
        old_soup = BeautifulSoup(old.text, "html.parser")
        target = self._find_version_link(old_soup, version)
        if not target:
            raise ProviderError(f"APKCombo version not found: {version}")
        version_page = self.http.get(urljoin(old.url, target))
        version_soup = BeautifulSoup(version_page.text, "html.parser")
        download_url = self._download_url(version_soup, version_page.url, arch)
        extension = ".xapk" if ".xapk" in version_page.text.lower() and not download_url.endswith(".apk") else ".apk"
        return Artifact(self.name, package, version, download_url, extension, arch)

    def _find_app(self, package: str) -> str:
        # APKCombo's canonical application URL contains both a slug and package.
        search = self.http.get(f"https://apkcombo.com/search?q={quote(package, safe='')}")
        soup = BeautifulSoup(search.text, "html.parser")
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            if f"/{package}/" in href and "apkcombo.com" in href:
                return urljoin(search.url, href).rstrip("/")
        raise ProviderError(f"APKCombo app not found for package {package}")

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
        target = self._same_version
        for link in soup.select("a[href]"):
            text = link.get_text(" ", strip=True)
            href = link.get("href", "")
            if target(text, version):
                return href
        # Search embedded structured data as a fallback.
        for script in soup.select("script"):
            if version in script.get_text():
                match = re.search(r'https?://[^"\\s]+', script.get_text())
                if match:
                    return match.group(0)
        return None

    def _download_url(self, soup: BeautifulSoup, page_url: str, arch: str | None) -> str:
        candidates = []
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            text = link.get_text(" ", strip=True).lower()
            if "/download/" in href and ("apk" in text or "apk" in href):
                candidates.append(urljoin(page_url, href))
        if not candidates:
            install = soup.find("link", rel="alternate")
            if install and install.get("href"):
                candidates.append(urljoin(page_url, install["href"]))
        if not candidates:
            raise ProviderError("APKCombo did not expose a download link")
        return candidates[0]
    
    @staticmethod
    def _same_version(left: str | None, right: str | None) -> bool:
        if not left or not right:
            return False
        clean = lambda v: re.sub(r"[\s\-()]+", "", v).lower()
        return clean(left) == clean(right)
