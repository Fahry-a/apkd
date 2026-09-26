from __future__ import annotations

from .base import Provider, normalize_arch
from ..models import Artifact, DownloadRequest, ProviderError


class AptoideProvider(Provider):
    name = "aptoide"
    capabilities = {"version": True, "arch": False, "dpi": False, "min_sdk": False}
    api = "https://ws75.aptoide.com/api/7"

    def resolve_request(self, request: DownloadRequest) -> Artifact:
        package = request.package
        version = request.version
        arch = normalize_arch(request.arch)
        self.http.timeout = request.timeout
        if version is None:
            item = self._search(package)
        else:
            item = self._find_version(package, version)
        file_info = item.get("file") or {}
        url = file_info.get("path") or file_info.get("path_alt")
        actual_version = str(file_info.get("vername") or version or "")
        if not url and actual_version:
            # listAppsVersions returns metadata but not the binary URL for some
            # stores.  getApp exposes the same version's file path when it is
            # still available from that store.
            hydrated = self._get_app_file(package, actual_version)
            if hydrated:
                file_info = hydrated
                url = hydrated.get("path") or hydrated.get("path_alt")
        if not url:
            raise ProviderError(
                f"Aptoide returned no APK URL for {package} {actual_version}",
                provider=self.name,
            )
        return Artifact(self.name, package, actual_version, url, ".apk", arch)

    def _search(self, package: str) -> dict:
        data = self._get_json("/apps/search", {"query": package, "limit": 20})
        for item in (data.get("datalist") or {}).get("list") or []:
            if item.get("package") == package:
                return item
        raise ProviderError(f"Aptoide app not found: {package}", provider=self.name)

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
        raise ProviderError(f"Aptoide version not found: {package} {version}", provider=self.name)

    def _get_app_file(self, package: str, version: str) -> dict | None:
        """Fetch current metadata and select only an exact file entry."""
        try:
            data = self._post_json("/getApp", {"package_name": package})
        except Exception:
            return None

        candidates: list[dict] = []
        nodes = data.get("nodes") if isinstance(data, dict) else None
        if isinstance(nodes, dict):
            meta = ((nodes.get("meta") or {}).get("data") or {})
            if isinstance(meta.get("file"), dict):
                candidates.append(meta["file"])
            versions = ((nodes.get("versions") or {}).get("list") or [])
            for entry in versions:
                if isinstance(entry, dict) and isinstance(entry.get("file"), dict):
                    candidates.append(entry["file"])
        for file_info in candidates:
            if str(file_info.get("vername") or "") == version:
                return file_info
        # Never hydrate an arbitrary current file when the requested version
        # cannot be identified exactly. The wrapper validates the returned
        # version, but a provider-level fallback must not make that unsafe.
        return None

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
