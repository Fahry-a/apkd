"""Android APK downloader with pluggable mirror providers."""

# Declared before the re-exports: apkd.http reads it while building its
# User-Agent, and the submodules must not import the partially-initialised
# package.
__version__ = "0.2.0"

from .fallback import download, download_simple
from .models import Artifact, DownloadError, DownloadRequest, DownloadResult, ProviderError
from .validation import validate_package

__all__ = [
    "Artifact",
    "DownloadError",
    "DownloadRequest",
    "DownloadResult",
    "ProviderError",
    "download",
    "download_simple",
    "validate_package",
]
