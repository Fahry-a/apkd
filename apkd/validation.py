from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from .providers.base import required_abis

APK_EXTENSIONS = {".apk", ".xapk", ".apkm", ".apks"}
BUNDLE_EXTENSIONS = {".apkm", ".xapk", ".apks"}

# Smallest plausible Android package. Anything under this is an error page or a
# truncated stream, not an artifact.
MIN_PACKAGE_BYTES = 4096

# Reading a bundle entry means decompressing it. Cap the total so a hostile or
# corrupt "bundle" cannot exhaust memory during validation.
MAX_BUNDLE_ENTRIES = 64
MAX_ENTRY_BYTES = 512 * 1024 * 1024


def _native_abis_from_apk(apk_source) -> set[str]:
    with zipfile.ZipFile(apk_source) as archive:
        return {
            parts[1]
            for name in archive.namelist()
            if name.startswith("lib/")
            for parts in [name.split("/", 2)]
            if len(parts) > 1
        }


def _bundle_apk_entries(archive: zipfile.ZipFile) -> list[str]:
    """The APK entries of a bundle, bounded so a hostile file cannot pile up."""
    entries = [n for n in archive.namelist() if n.lower().endswith(".apk")]
    if len(entries) > MAX_BUNDLE_ENTRIES:
        raise ValueError(
            f"bundle holds {len(entries)} APK entries, more than the "
            f"{MAX_BUNDLE_ENTRIES} this validator accepts"
        )
    for name in entries:
        size = archive.getinfo(name).file_size
        if size > MAX_ENTRY_BYTES:
            raise ValueError(
                f"bundle entry {name} declares {size} bytes, over the "
                f"{MAX_ENTRY_BYTES} byte inspection limit"
            )
    return entries


def _native_abis(path: Path, suffix: str) -> set[str]:
    if suffix == ".apk":
        return _native_abis_from_apk(path)
    detected: set[str] = set()
    inspection_errors: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in _bundle_apk_entries(archive):
            try:
                detected.update(_native_abis_from_apk(archive.open(name)))
            except zipfile.BadZipFile as exc:
                inspection_errors.append(f"{name}: {exc}")
    if inspection_errors:
        raise ValueError(
            "could not inspect native ABIs in bundle entries: "
            + "; ".join(inspection_errors)
        )
    return detected


def _validate_architecture(path: Path, arch: str | None) -> None:
    required = required_abis(arch)
    if not required:
        return
    detected = _native_abis(path, path.suffix.lower())
    # No native libraries means the package is architecture-independent.
    if not detected:
        return
    if not required.issubset(detected):
        missing = ", ".join(sorted(required - detected))
        found = ", ".join(sorted(detected))
        raise ValueError(
            f"package does not satisfy arch={arch}: missing {missing}; "
            f"detected native ABIs: {found}"
        )


def _validate_sha256(path: Path, expected: str) -> None:
    if not expected:
        return
    wanted = str(expected).strip().lower()
    if len(wanted) != 64 or any(c not in "0123456789abcdef" for c in wanted):
        raise ValueError(f"provider reported an unusable sha256: {expected!r}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != wanted:
        raise ValueError(f"sha256 mismatch: expected {wanted}, got {actual}")


def validate_package(
    path: Path,
    *,
    expected_extension: str | None = None,
    expected_arch: str | None = None,
    expected_sha256: str | None = None,
) -> None:
    """Reject anything that is not a ZIP-based Android package we asked for."""
    if not path.is_file():
        raise ValueError(f"package does not exist: {path}")
    if path.stat().st_size < MIN_PACKAGE_BYTES:
        raise ValueError(
            f"package is suspiciously small: {path} "
            f"({path.stat().st_size} bytes, minimum {MIN_PACKAGE_BYTES})"
        )

    suffix = path.suffix.lower()
    if expected_extension and suffix != expected_extension.lower():
        raise ValueError(f"unexpected extension: {suffix}, expected {expected_extension}")
    if suffix not in APK_EXTENSIONS:
        raise ValueError(f"unsupported Android package extension: {suffix}")

    if not zipfile.is_zipfile(path):
        raise ValueError("downloaded file is not a valid ZIP-based Android package")

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        has_manifest = "AndroidManifest.xml" in names
        if not has_manifest:
            # Bound the bundle before trusting its shape, so an over-large or
            # over-many entry set is rejected even when no arch was requested.
            entries = _bundle_apk_entries(archive) if suffix in BUNDLE_EXTENSIONS else []
            if not entries:
                raise ValueError("downloaded package has no AndroidManifest.xml")
    if expected_arch:
        _validate_architecture(path, expected_arch)
    _validate_sha256(path, expected_sha256)
