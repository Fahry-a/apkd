from __future__ import annotations

from .apkpure import APKPureProvider
from .apkmirror import APKMirrorProvider
from .aptoide import AptoideProvider
from .apkcombo import APKComboProvider
from .uptodown import UptodownProvider

PROVIDERS = {
    "apkcombo": APKComboProvider,
    "aptoide": AptoideProvider,
    "apkpure": APKPureProvider,
    "apkmirror": APKMirrorProvider,
    "uptodown": UptodownProvider,
}

# Uptodown is intentionally opt-in because its final download step requires
# an interactive browser Turnstile session.
PROVIDER_ORDER = ["apkpure", "aptoide", "apkcombo", "apkmirror"]


def get_provider(name: str, **kwargs):
    try:
        cls = PROVIDERS[name.lower()]
    except KeyError as exc:
        raise ValueError(f"provider is not implemented here: {name}") from exc
    return cls(**kwargs) if kwargs else cls()


def list_providers() -> dict[str, dict]:
    return {name: dict(cls.capabilities) for name, cls in PROVIDERS.items()}
