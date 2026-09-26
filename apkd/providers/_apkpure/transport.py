"""HTTP transport with TLS impersonation chain."""

try:
    from curl_cffi import requests as _creq
    _HTTPLIB = "curl_cffi"
except ImportError:
    import requests as _creq
    _HTTPLIB = "requests"

from .config import Config

# Preference order, best-matching an Android client first. Filtered against the
# installed curl_cffi at import time: a pinned dependency that drops a target
# would otherwise burn failed handshakes inside the retry loop below.
_PREFERRED_TARGETS = (
    "chrome131_android", "chrome_android", "chrome99_android",
    "chrome131", "chrome124", "chrome120",
)


def _supported_targets() -> list[str]:
    try:
        from curl_cffi.requests.impersonate import BrowserTypeLiteral
        import typing
        available = set(typing.get_args(BrowserTypeLiteral))
    except Exception:  # noqa: BLE001 - unknown literal set; use everything
        return list(_PREFERRED_TARGETS)
    usable = [target for target in _PREFERRED_TARGETS if target in available]
    return usable or ["chrome"]


IMPERSONATE_CHAIN = _supported_targets()


def supported_impersonation(preferred: tuple[str, ...]) -> list[str]:
    """``preferred`` filtered down to what the installed curl_cffi accepts."""
    available = _supported_targets()
    if len(available) == len(_PREFERRED_TARGETS):
        # Literal set could not be introspected; trust the caller's order.
        return list(preferred)
    return [target for target in preferred if target in available] or available


last_transport = None


def post(cfg: Config, url: str, headers: dict, body: bytes, timeout: int = 20):
    global last_transport
    if cfg.no_tls or _HTTPLIB == "requests":
        last_transport = last_transport or f"plain {_HTTPLIB}"
        return _creq.post(url, headers=headers, data=body, timeout=timeout)
    err = None
    for target in IMPERSONATE_CHAIN:
        try:
            r = _creq.post(url, headers=headers, data=body, timeout=timeout, impersonate=target)
            last_transport = f"impersonate:{target}"
            return r
        except Exception as e:
            err = e
    last_transport = f"plain requests ({repr(err)[:80]})"
    return _creq.post(url, headers=headers, data=body, timeout=timeout)