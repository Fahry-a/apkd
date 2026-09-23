from __future__ import annotations

from abc import ABC, abstractmethod

from ..http import HttpClient
from ..models import Artifact


class Provider(ABC):
    name: str

    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()

    @abstractmethod
    def resolve(self, package: str, version: str | None = None, arch: str | None = None) -> Artifact:
        raise NotImplementedError
