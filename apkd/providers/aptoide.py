from __future__ import annotations

from urllib.parse import urlencode

from .base import Provider
from ..models import Artifact, ProviderError


class AptoideProvider(Provider):
    name = "aptoide"
    api = "https://ws75.aptoide.com/api/7"

    def resolve(self, package: str, version: str | None = None, arch: str | None = None) -> Artifact:
        if version is None:
            item = self._search(package)
        else:
            item = self._find_version(package, version)
        file_info = item.get("file") or {}
        url = file_info.get("path") or file_info.get("path_alt")
        actual_version = str(file_info.get("vername") or version or "")
        if not url:
            raise ProviderError(f"Aptoide returned no APK URL for {package} {actual_version}")
        return Artifact(self.name, package, actual_version, url, ".apk", arch)

    def _search(self, package: str) -> dict:
        data = self._get_json("/apps/search", {"query": package, "limit": 20})
        for item in (data.get("datalist") or {}).get("list") or []:
            if item.get("package") == package:
                return item
        raise ProviderError(f"Aptoide app not found: {package}")

    def _find_version(self, package: str, version: str) -> dict:
        # Aptoide's v7 client exposes historical releases through listAppsVersions.
        for offset in range(0, 500, 100):
            data = self._post_json("/listAppsVersions", {
                "package_name": package,
                "limit": 100,
                "offset": offset,
            })
            entries = ((data.get("data") or {}).get("list")
                       if isinstance(data.get("data"), dict)
                       else data.get("list"))
            entries = entries or []
            for item in entries:
                file_info = item.get("file") or {}
                if str(file_info.get("vername", "")) == version:
                    return item
            if len(entries) < 100:
                break
        # Fallback: exact version-code query if the server returned the item id.
        raise ProviderError(f"Aptoide version not found: {package} {version}")

    def _get_json(self, path: str, params: dict) -> dict:
        response = self.http.get(f"{self.api}{path}", params=params)
        return response.json()

    def _post_json(self, path: str, payload: dict) -> dict:
        response = self.http.post(
            f"{self.api}{path}",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        return response.json()
