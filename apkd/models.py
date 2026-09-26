from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Artifact:
    provider: str
    package: str
    version: str
    url: str
    extension: str = ".apk"
    arch: Optional[str] = None
    extra: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class DownloadRequest:
    """Library-level download request shared by all providers."""

    package: str
    version: Optional[str] = None
    arch: Optional[str] = None
    dpi: Optional[str] = None
    min_sdk: Optional[int] = None
    prefer_xapk: bool = False
    timeout: float = 30.0
    # Optional provider-specific page hint (for example an APKPure slug).
    # It is appended so existing positional DownloadRequest calls remain safe.
    app_slug: Optional[str] = None
    # Optional Uptodown application ID, used to avoid a fragile app-page
    # lookup when a mirror config already knows the stable numeric ID.
    app_id: Optional[str] = None


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    provider: str
    package: str
    version: str
    arch: Optional[str]
    url: str
    extension: str = ".apk"


class ProviderError(RuntimeError):
    """Raised when a provider cannot resolve or download an artifact."""

    def __init__(self, message: str, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class DownloadError(RuntimeError):
    """Raised when every provider in the fallback chain fails."""

    def __init__(self, message: str, provider_errors: dict[str, BaseException] | None = None) -> None:
        super().__init__(message)
        self.provider_errors: dict[str, BaseException] = dict(provider_errors or {})
