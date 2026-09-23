from __future__ import annotations

import zipfile
from pathlib import Path


APK_EXTENSIONS = {".apk", ".xapk", ".apkm", ".apks"}


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
        if "AndroidManifest.xml" not in names:
            raise ValueError("downloaded package has no AndroidManifest.xml")
