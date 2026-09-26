from __future__ import annotations

from pathlib import Path
from typing import Iterator

import requests


from . import __version__

USER_AGENT = (
    f"apkd/{__version__} (+https://github.com/Fahry-a/apkd) "
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131 Safari/537.36"
)

STREAM_CHUNK_BYTES = 256 * 1024
# Smallest plausible Android package; anything under this is an error page.
MIN_PACKAGE_BYTES = 4096


class HttpClient:
    def __init__(self, timeout: float = 30.0) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.timeout = timeout

    def get(self, url: str, **kwargs) -> requests.Response:
        response = self.session.get(url, timeout=self.timeout, **kwargs)
        response.raise_for_status()
        return response

    def post(self, url: str, **kwargs) -> requests.Response:
        response = self.session.post(url, timeout=self.timeout, **kwargs)
        response.raise_for_status()
        return response

    def download(self, url: str, destination: Path, *, min_bytes: int = MIN_PACKAGE_BYTES) -> int:
        with self.session.get(url, stream=True, timeout=self.timeout, allow_redirects=True) as response:
            response.raise_for_status()
            written = 0
            with destination.open("wb") as output:
                for chunk in response.iter_content(chunk_size=STREAM_CHUNK_BYTES):
                    if chunk:
                        output.write(chunk)
                        written += len(chunk)
        if written < min_bytes:
            destination.unlink(missing_ok=True)
            raise ValueError(f"downloaded file is suspiciously small: {written} bytes")
        return written
