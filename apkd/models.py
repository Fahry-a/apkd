from __future__ import annotations

from dataclasses import dataclass
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


class ProviderError(RuntimeError):
    """Raised when a provider cannot resolve or download an artifact."""
