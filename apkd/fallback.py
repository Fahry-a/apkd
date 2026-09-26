from __future__ import annotations

import logging
import re
from pathlib import Path

from .models import DownloadError, DownloadRequest, DownloadResult
from .providers import PROVIDER_ORDER, get_provider
from .validation import validate_package

log = logging.getLogger("apkd.fallback")

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._+-]")


def _safe_component(value: str, fallback: str = "unknown") -> str:
    """Reduce a string to a single harmless path component.

    Package and version strings reach the filesystem as a default filename, so
    an untrusted package name such as ``../../etc/cron.d/x`` must not be able to
    steer the write outside the working directory.
    """
    cleaned = _UNSAFE_NAME.sub("_", str(value or "").strip())
    cleaned = cleaned.strip("._") or fallback
    return cleaned[:120]


def _coerce_output(request: DownloadRequest, artifact, output: Path | str | None) -> Path:
    version = _safe_component(artifact.version or request.version or "", "latest")
    package = _safe_component(request.package, "package")
    default_name = f"{package}_{version}{artifact.extension}"
    if output is None:
        return Path(default_name)
    out = Path(output)
    if out.suffix.lower() == artifact.extension.lower():
        return out
    if out.exists() and out.is_dir():
        return out / default_name
    # The container type has to be right: callers and validation both key off
    # the extension, so a `.apk` request for a bundle is written as `.xapk`.
    corrected = out.with_suffix(artifact.extension)
    if out.suffix:
        log.info("output %s rewritten to %s for the %s container", out, corrected, artifact.extension)
    return corrected


def download(
    request: DownloadRequest,
    providers: list[str] | None = None,
    output: Path | str | None = None,
) -> DownloadResult:
    """Try each provider in order until one resolves and downloads successfully."""
    order = [p.lower() for p in (providers or PROVIDER_ORDER)]
    if not order:
        raise ValueError("provider list is empty")
    errors: dict[str, BaseException] = {}
    for name in order:
        log.debug("trying provider %s", name)
        try:
            provider = get_provider(name)
        except Exception as exc:  # noqa: BLE001 - one bad name must not stop the chain
            errors[name] = exc
            continue
        if request.timeout:
            provider.http.timeout = request.timeout
        try:
            artifact = provider.resolve_request(request)
            dest = _coerce_output(request, artifact, output)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.parent / (dest.stem + ".part" + dest.suffix)
            try:
                provider.download(artifact, tmp)
                validate_package(
                    tmp,
                    expected_extension=artifact.extension,
                    expected_arch=request.arch,
                    expected_sha256=(artifact.extra or {}).get("sha256"),
                )
                tmp.replace(dest)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            log.debug("provider %s resolved %s %s", name, artifact.package, artifact.version)
            return DownloadResult(
                path=dest,
                provider=artifact.provider,
                package=artifact.package,
                version=artifact.version,
                arch=artifact.arch,
                url=artifact.url,
                extension=artifact.extension,
            )
        except Exception as exc:  # noqa: BLE001 - aggregate per-provider failures
            log.debug("provider %s failed: %s", name, exc)
            errors[name] = exc
    raise DownloadError(
        f"all providers failed for {request.package} {request.version or 'latest'}: "
        + ", ".join(f"{k}={v}" for k, v in errors.items()),
        provider_errors=errors,
    )


def download_simple(
    package: str,
    version: str | None = None,
    arch: str | None = None,
    dpi: str | None = None,
    min_sdk: int | None = None,
    prefer_xapk: bool = False,
    providers: list[str] | None = None,
    output: Path | str | None = None,
    timeout: float = 30.0,
) -> DownloadResult:
    return download(
        DownloadRequest(
            package=package,
            version=version,
            arch=arch,
            dpi=dpi,
            min_sdk=min_sdk,
            prefer_xapk=prefer_xapk,
            timeout=timeout,
        ),
        providers=providers,
        output=output,
    )
