from __future__ import annotations

import io
import zipfile
from pathlib import Path


APK_EXTENSIONS = {".apk", ".xapk", ".apkm", ".apks"}


def _contains_manifest(data: bytes) -> bool:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        return False
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return "AndroidManifest.xml" in archive.namelist()


def validate_package(path: Path, *, expected_extension: str | None = None) -> None:
    if not path.is_file():
        raise ValueError(f"package does not exist: {path}")
    if path.stat().st_size < 4096:
        raise ValueError(f"package is suspiciously small: {path}")

    suffix = path.suffix.lower()
    if expected_extension and suffix != expected_extension.lower():
        raise ValueError(f"unexpected extension: {suffix}, expected {expected_extension}")
    if suffix not in APK_EXTENSIONS:
        raise ValueError(f"unsupported Android package extension: {suffix}")

    if not zipfile.is_zipfile(path):
        raise ValueError("downloaded file is not a valid ZIP-based Android package")

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if suffix == ".apk":
            if "AndroidManifest.xml" not in names:
                raise ValueError("downloaded APK has no AndroidManifest.xml")
            return

        nested_apks = [name for name in names if name.lower().endswith(".apk")]
        if not any(_contains_manifest(archive.read(name)) for name in nested_apks):
            raise ValueError(f"downloaded {suffix} has no valid APK containing AndroidManifest.xml")
