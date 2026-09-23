from __future__ import annotations

import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from .base import Provider
from ..models import Artifact, ProviderError


class UptodownProvider(Provider):
    name = "uptodown"
    locales = ("en", "in", "de", "fr", "it", "ru", "jp", "kr")

    def resolve(self, package: str, version: str | None = None, arch: str | None = None) -> Artifact:
        app_url = self._find_app(package)
        versions_url = app_url.rstrip("/") + "/versions"
        page = self.http.get(versions_url)
        soup = BeautifulSoup(page.text, "html.parser")
        heading = soup.select_one("#detail-app-name")
        if not heading:
            raise ProviderError("Uptodown app page has no application metadata")
        data_code = heading.get("data-code")
        if not data_code:
            raise ProviderError("Uptodown app page has no data-code")

        if version is None:
            version = self._latest_version(soup)
        entry = self._find_version(app_url, data_code, version)
        parts = entry.get("versionURL") or {}
        version_url = "/".join(str(parts.get(k, "")).strip("/") for k in ("url", "extraURL", "versionID"))
        if not version_url.startswith("http"):
            raise ProviderError(f"Uptodown returned an invalid version URL for {version}")

        version_page = self.http.get(version_url)
        vsoup = BeautifulSoup(version_page.text, "html.parser")
        file_id = self._pick_variant(app_url, data_code, parts.get("versionID"), vsoup, arch)
        if file_id:
            variant_page = self.http.get(f"{app_url.rstrip('/')}/download/{file_id}-x")
            vsoup = BeautifulSoup(variant_page.text, "html.parser")

        button = vsoup.select_one("#detail-download-button")
        if not button or not button.get("data-url"):
            raise ProviderError(f"Uptodown did not expose a direct download URL for {version}")
        token = button["data-url"]
        url = urljoin("https://dw.uptodown.com/dwn/", token)
        extension = ".xapk" if str(entry.get("kindFile", "")).lower() == "xapk" else ".apk"
        return Artifact(self.name, package, version, url, extension, arch)

    def _find_app(self, package: str) -> str:
        search_url = f"https://en.uptodown.com/android/search/{quote(package, safe='')}"
        response = self.http.get(search_url)
        soup = BeautifulSoup(response.text, "html.parser")
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            absolute = urljoin(response.url, href)
            if ".uptodown.com/android/" in absolute and "/search/" not in absolute:
                return absolute.rstrip("/")
        # Some locales expose the search page under a locale-specific host.
        for locale in self.locales[1:]:
            response = self.http.get(
                f"https://{locale}.uptodown.com/android/search/{quote(package, safe='')}"
            )
            soup = BeautifulSoup(response.text, "html.parser")
            for link in soup.select("a[href]"):
                href = link.get("href", "")
                absolute = urljoin(response.url, href)
                if ".uptodown.com/android/" in absolute and "/search/" not in absolute:
                    return absolute.rstrip("/")
        raise ProviderError(f"Uptodown app not found for package {package}")

    def _latest_version(self, soup: BeautifulSoup) -> str:
        node = soup.select_one("[itemprop='softwareVersion'], .version")
        if not node:
            raise ProviderError("Uptodown did not expose a version")
        value = node.get_text(" ", strip=True)
        if not value:
            raise ProviderError("Uptodown returned an empty version")
        return value

    def _find_version(self, app_url: str, data_code: str, version: str) -> dict:
        target = self._normalize(version)
        for page_number in range(1, 21):
            response = self.http.get(
                f"{app_url.rstrip('/')}/apps/{data_code}/versions/{page_number}"
            )
            payload = response.json()
            entries = payload.get("data") or []
            if not entries:
                break
            for entry in entries:
                if self._normalize(str(entry.get("version", ""))) == target:
                    return entry
        raise ProviderError(f"Uptodown version not found: {version}")

    def _pick_variant(self, app_url: str, data_code: str, version_id: object,
                      version_soup: BeautifulSoup, arch: str | None) -> str | None:
        button = version_soup.select_one(".button.variants[data-version]")
        if not button:
            return None
        data_version = button.get("data-version") or version_id
        if not data_version:
            return None
        files_url = f"{app_url.rsplit('/android', 1)[0]}/app/{data_code}/version/{data_version}/files"
        response = self.http.get(files_url)
        content = (response.json() or {}).get("content", "")
        soup = BeautifulSoup(content, "html.parser")
        wanted = {"arm-v7a": "armeabi-v7a", "arm32": "armeabi-v7a"}.get(arch or "", arch)
        current_arch = ""
        fallback = None
        for child in soup.select(".content > *"):
            if child.name == "p":
                current_arch = child.get_text(" ", strip=True).lower()
                continue
            if "variant" not in (child.get("class") or []):
                continue
            report = child.select_one(".v-report[data-file-id]")
            if not report:
                continue
            file_id = report.get("data-file-id")
            if fallback is None:
                fallback = file_id
            if wanted and wanted in current_arch:
                return file_id
            if not wanted:
                return file_id
        return fallback

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[\[\(].*?[\]\)]", "", value).strip()
