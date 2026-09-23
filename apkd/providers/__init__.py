from __future__ import annotations

from .aptoide import AptoideProvider
from .apkcombo import APKComboProvider
from .uptodown import UptodownProvider

PROVIDERS = {
    "uptodown": UptodownProvider,
    "apkcombo": APKComboProvider,
    "aptoide": AptoideProvider,
}


def get_provider(name: str):
    try:
        return PROVIDERS[name.lower()]()
    except KeyError as exc:
        raise ValueError(f"provider is not implemented here: {name}") from exc
