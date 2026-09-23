from __future__ import annotations

import re
from urllib.parse import quote, unquote, urljoin

from bs4 import BeautifulSoup

from .base import Provider
from ..models import Artifact, ProviderError


class UptodownProvider(Provider):
    name = "uptodown"

    def resolve(self, package: str, version: str | None = None, arch: str | None = None) -> Artifact:
        app_url = self._find_app(package)
        if version is None:
            page = self.http.get(app_url)
            soup = BeautifulSoup(page.text, "html.parser")
            version = self._page_version(soup)
        else:
            page = self._find_version_page(app_url, version)
            soup = BeautifulSoup(page.text, "html.parser")

        download_url = self._download_url(app_url, page.url, soup)
        extension = self._extension(soup)
        return Artifact(self.name, package, version, download_url, extension, arch)

    def _find_app(self, package: str) -> str:
        # Uptodown changed /android/search/<package> to a POST search endpoint.
        response = self.http.post(
            "https://en.uptodown.com/android/search",
            data={"singlebutton": "", "q": package},
            headers={"Referer": "https://en.uptodown.com/"},
        )
        soup = BeautifulSoup(response.text, "html.parser")
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            absolute = urljoin(response.url, href)
            if ".uptodown.com/android/" in absolute and "/search" not in absolute:
                return absolute.rstrip("/")

        raise ProviderError(f"Uptodown app not found for package {package}")

    def _find_version_page(self, app_url: str, version: str):
        versions = self.http.get(app_url.rstrip("/") + "/versions")
        soup = BeautifulSoup(versions.text, "html.parser")
        target = self._normalize(version)

        for link in soup.select("a[href]"):
            text = link.get_text(" ", strip=True)
            version_node = link.select_one(".app_card_version")
            candidates = [text]
            if version_node:
                candidates.append(version_node.get_text(" ", strip=True))
            if any(self._normalize(value) == target for value in candidates if value):
                href = link.get("href")
                if href:
                    return self.http.get(urljoin(versions.url, href))

        # The versions page can expose a version directly in a data attribute.
        for node in soup.select("[data-version][href], a[data-version]"):
            if self._normalize(node.get("data-version", "")) == target:
                href = node.get("href")
                if href:
                    return self.http.get(urljoin(versions.url, href))

        raise ProviderError(f"Uptodown version not found: {version}")

    def _download_url(self, app_url: str, page_url: str, soup: BeautifulSoup) -> str:
        button = soup.select_one("#detail-download-button[data-url]")
        if button:
            return "https://dw.uptodown.com/dwn/" + button["data-url"].lstrip("/")

        heading = soup.select_one("#detail-app-name[data-file-id]")
        if not heading:
            raise ProviderError("Uptodown download page has no file id")

        file_id = heading["data-file-id"]
        download_page = self.http.get(f"{app_url.rstrip('/')}/download/{file_id}-x")
        download_soup = BeautifulSoup(download_page.text, "html.parser")
        button = download_soup.select_one("#detail-download-button[data-url]")
        if button:
            return "https://dw.uptodown.com/dwn/" + button["data-url"].lstrip("/")

        post_download = self.http.get(f"{app_url.rstrip('/')}/post-download/{file_id}")
        post_soup = BeautifulSoup(post_download.text, "html.parser")
        node = post_soup.select_one("[data-url]")
        if node and node.get("data-url"):
            return "https://dw.uptodown.com/dwn/" + node["data-url"].lstrip("/")

        raise ProviderError(f"Uptodown did not expose a direct download URL for {page_url}")

    @staticmethod
    def _page_version(soup: BeautifulSoup) -> str:
        node = soup.select_one("[itemprop='softwareVersion'], .version, div.version")
        if not node:
            raise ProviderError("Uptodown did not expose a version")
        value = node.get_text(" ", strip=True)
        if not value:
            raise ProviderError("Uptodown returned an empty version")
        return value.lstrip("v").strip()

    @staticmethod
    def _extension(soup: BeautifulSoup) -> str:
        text = soup.get_text(" ", strip=True).lower()
        match = re.search(r"file type\\s+(apk|xapk|apkm|apks)", text)
        if match:
            return "." + match.group(1)
        return ".apk"

    @staticmethod
    def _normalize(value: str) -> str:
        value = re.sub(r"[\\[\\(].*?[\\]\\)]", "", value)
        return re.sub(r"^v", "", value.strip(), flags=re.IGNORECASE)
