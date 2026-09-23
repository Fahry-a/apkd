from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import quote

from bs4 import BeautifulSoup

from .base import Provider
from ..models import Artifact, ProviderError


_API_BASE = "https://www.uptodown.app/eapi"
_APIKEY_SECRET = "$(=a%·!45J&S"
_API_UA = "Dalvik/2.1.0 (Linux; U; Android 14; SM-G955F Build/AP2A.240805.005)"


class UptodownProvider(Provider):
    name = "uptodown"

    def resolve(self, package: str, version: str | None = None, arch: str | None = None) -> Artifact:
        app_id = self._resolve_app_id(package)
        if not app_id:
            raise ProviderError(f"Uptodown app not found: {package}")

        versions = self._get_versions(app_id)
        if not versions:
            raise ProviderError(f"Uptodown returned no versions for {package}")

        target = self._select_version(versions, version)
        actual_version = str(target.get("version") or target.get("versionCode") or "")
        file_id = str(target.get("fileID") or target.get("fileid") or "")
        file_type = str(target.get("fileType") or target.get("filetype") or "apk").lower()

        if not actual_version or not file_id:
            raise ProviderError(f"Uptodown returned incomplete version metadata for {package}")

        if version is not None and self._normalize(actual_version) != self._normalize(version):
            raise ProviderError(
                f"Uptodown returned version {actual_version}, requested {version}"
            )

        response = self._api_get(
            f"/apps/{quote(app_id, safe='')}/file/{quote(file_id, safe='')}/downloadUrl?update=0"
        )
        if response.status_code != 200:
            raise ProviderError(
                f"Uptodown download URL request failed: HTTP {response.status_code}"
            )

        data = response.json()
        url = (data.get("data") or {}).get("downloadURL")
        if not url:
            raise ProviderError(f"Uptodown returned no download URL for {package} {actual_version}")

        extension = "." + file_type if file_type in {"apk", "xapk", "apkm", "apks"} else ".apk"
        return Artifact(self.name, package, actual_version, url, extension, arch)

    def _resolve_app_id(self, package: str) -> str | None:
        response = self._api_get(f"/apps/byPackagename/{quote(package, safe='')}")
        if response.status_code == 200:
            data = response.json()
            inner = data.get("data", data)
            app_id = inner.get("appID") or inner.get("id")
            if app_id:
                return str(app_id)

        response = self._api_get(
            f"/v2/apps/search/{quote(package, safe='')}?page[limit]=30&page[offset]=0"
        )
        if response.status_code != 200:
            return None

        data = response.json()
        items = (data.get("data") or {}).get("results") or []
        for item in items:
            item_package = item.get("packageName") or item.get("packagename") or ""
            if item_package == package:
                app_id = item.get("appID") or item.get("id")
                return str(app_id) if app_id else None
        return None

    def _get_versions(self, app_id: str, limit: int = 100) -> list[dict]:
        response = self._api_get(
            f"/v3/app/{quote(app_id, safe='')}/device/1/compatible/versions"
            f"?page[limit]={limit}&page[offset]=0"
        )
        if response.status_code != 200:
            return []
        data = response.json()
        versions = data.get("data", [])
        return versions if isinstance(versions, list) else []

    @staticmethod
    def _select_version(versions: list[dict], requested: str | None) -> dict:
        if requested is None:
            return versions[0]

        target = UptodownProvider._normalize(requested)
        for item in versions:
            actual = str(item.get("version") or "")
            if UptodownProvider._normalize(actual) == target:
                return item

        available = ", ".join(str(v.get("version", "?")) for v in versions[:5])
        raise ProviderError(
            f"Uptodown version not found: {requested}. Available: {available}"
        )

    def _api_get(self, path: str):
        return self.http.session.get(
            f"{_API_BASE}{path}",
            headers=self._api_headers(),
            timeout=self.http.timeout,
        )

    @classmethod
    def _api_headers(cls) -> dict[str, str]:
        now = datetime.now(UTC)
        hour_epoch = int(now.timestamp()) // 3600 * 3600
        digest = hashlib.sha256(
            f"{_APIKEY_SECRET}{hour_epoch}".encode("utf-8")
        ).hexdigest()
        return {
            "User-Agent": _API_UA,
            "Identificador": "Uptodown_Android",
            "Identificador-Version": "707",
            "APIKEY": digest,
            "Accept-Language": "en-US,en;q=0.9",
        }

    @staticmethod
    def _normalize(value: str) -> str:
        value = re.sub(r"[\[\(].*?[\]\)]", "", value)
        return re.sub(r"^v", "", value.strip(), flags=re.IGNORECASE)
