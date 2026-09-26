from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from ..http import HttpClient
from ..models import Artifact, DownloadRequest

ARCH_ALIASES = {
    "arm-v7a": "armeabi-v7a",
    "arm32": "armeabi-v7a",
    "armeabi": "armeabi-v7a",
    "arm64": "arm64-v8a",
    "aarch64": "arm64-v8a",
    "arm64v8a": "arm64-v8a",
    "armeabi-v7a": "armeabi-v7a",
    "arm64-v8a": "arm64-v8a",
    "x86": "x86",
    "x86_64": "x86_64",
    "x64": "x86_64",
    "universal": "universal",
    "noarch": "noarch",
    "nodpi": "nodpi",
}

# Canonical ABI -> the spellings providers use for it. Shared so the
# universal-ABI contract has exactly one definition across the package.
ABI_SPELLINGS: dict[str, tuple[str, ...]] = {
    "arm64-v8a": ("arm64-v8a", "arm64", "aarch64", "arm64_v8a"),
    "armeabi-v7a": ("armeabi-v7a", "arm-v7a", "arm32", "armeabi", "armeabi_v7a"),
    "x86_64": ("x86_64", "x86-64", "x64"),
    "x86": ("x86", "x86_32"),
}

# `universal` is a strict contract: a native package must carry both ARM ABIs
# (or no native code at all). Single-ABI assets are never relabelled as such.
UNIVERSAL_ABIS: tuple[str, ...] = ("arm64-v8a", "armeabi-v7a")


# The ABIs `arch` may name, beyond the two universal requires. `x86` spellings
# exist so a match can succeed; they are never part of the universal contract.
_KNOWN_ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64", "x86")


def normalize_arch(arch: str | None) -> str | None:
    if not arch:
        return None
    key = arch.strip().lower().replace("_", "-")
    return ARCH_ALIASES.get(key, key)


def required_abis(arch: str | None) -> set[str]:
    """The canonical ABIs a package must contain to satisfy ``arch``.

    ``universal`` is strict: both ARM ABIs. A name this table does not know is
    passed through, so a caller asking for an exotic ABI still gets it checked
    rather than silently skipped.
    """
    normalized = normalize_arch(arch)
    if not normalized:
        return set()
    if normalized in ("universal", "noarch"):
        return set(UNIVERSAL_ABIS)
    if normalized in _KNOWN_ABIS:
        return {normalized}
    return {normalized}


def abi_spellings(arch: str) -> tuple[str, ...]:
    """Every spelling providers use for ``arch`` (a match, never a contract)."""
    normalized = normalize_arch(arch) or arch
    if normalized in ("universal", "noarch"):
        spellings: list[str] = []
        for abi in UNIVERSAL_ABIS:
            spellings.extend(ABI_SPELLINGS[abi])
        return tuple(spellings)
    return ABI_SPELLINGS.get(normalized, (normalized,))


def matches_abi(haystack: str, arch: str) -> bool:
    """Whether an asset's context text advertises ``arch``."""
    text = haystack.lower()
    return any(spelling.lower() in text for spelling in abi_spellings(arch))


class Provider(ABC):
    name: str
    capabilities: ClassVar[dict[str, bool]] = {
        "version": True,
        "arch": False,
        "dpi": False,
        "min_sdk": False,
    }

    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()

    @abstractmethod
    def resolve_request(self, request: DownloadRequest) -> Artifact:
        raise NotImplementedError

    def resolve(
        self,
        package: str,
        version: str | None = None,
        arch: str | None = None,
    ) -> Artifact:
        """Legacy shim kept for backward compatibility."""
        return self.resolve_request(DownloadRequest(package=package, version=version, arch=arch))

    def download(self, artifact: Artifact, destination: Path) -> int:
        """Download an artifact. Providers with a custom transport override this."""
        return self.http.download(artifact.url, destination)
