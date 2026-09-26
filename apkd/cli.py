from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .fallback import download
from .models import DownloadError, DownloadRequest
from .providers import PROVIDER_ORDER, get_provider, list_providers

log = logging.getLogger("apkd")

# A provider must answer with an artifact the caller can actually use, or the
# chain moves on. These map to distinct exit codes so scripts can branch.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NO_PROVIDER = 3
EXIT_INVALID_ARTIFACT = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apkd", description="Download Android APKs from mirror providers."
    )
    sub = parser.add_subparsers(dest="command")

    dl = sub.add_parser("download", help="Download with provider fallback")
    _add_download_arguments(dl, output_required=False)
    dl.add_argument(
        "--providers",
        default=",".join(PROVIDER_ORDER),
        help=f"Comma-separated provider order (default: {','.join(PROVIDER_ORDER)})",
    )

    sub.add_parser("providers", help="List providers and capabilities")
    return parser


def _add_download_arguments(parser: argparse.ArgumentParser, *, output_required: bool) -> None:
    """Flags shared by ``download`` and the deprecated single-provider form."""
    parser.add_argument("package", help="Android package name, e.g. com.example.app")
    parser.add_argument("--version", help="Exact version; omit to resolve the provider's latest version")
    parser.add_argument("--arch", help="Requested ABI where supported (e.g. armeabi-v7a, arm64-v8a)")
    parser.add_argument("--dpi", help="Requested DPI where supported (e.g. 480dpi, nodpi)")
    parser.add_argument("--min-sdk", type=int, default=None, help="Minimum Android SDK where supported")
    parser.add_argument("--prefer-xapk", action="store_true", help="Prefer XAPK/bundle when available")
    parser.add_argument("--app-slug", help="Provider page slug when the canonical one is known")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout in seconds")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Log provider attempts to stderr"
    )
    parser.add_argument("-o", "--output", type=Path, required=output_required)


def _legacy_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apkd <provider>",
        description="Download an Android APK from a single provider (deprecated).",
    )
    parser.add_argument("provider", choices=sorted(list_providers()))
    _add_download_arguments(parser, output_required=True)
    return parser


def _request_from(args: argparse.Namespace) -> DownloadRequest:
    return DownloadRequest(
        package=args.package,
        version=args.version,
        arch=args.arch,
        dpi=args.dpi,
        min_sdk=args.min_sdk,
        prefer_xapk=args.prefer_xapk,
        timeout=args.timeout,
        app_slug=getattr(args, "app_slug", None),
    )


def _report(result) -> None:
    print(f"provider={result.provider} version={result.version} url={result.url}")
    print(f"file={result.path} size={result.path.stat().st_size}")


def _run(request: DownloadRequest, providers: list[str], output: Path | None) -> int:
    try:
        result = download(request, providers=providers, output=output)
    except DownloadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        for name, cause in exc.provider_errors.items():
            print(f"  {name}: {type(cause).__name__}: {cause}", file=sys.stderr)
        return EXIT_NO_PROVIDER
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID_ARTIFACT
    _report(result)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # Deprecated single-provider form: `apkd apkpure com.pkg -o file.apk`.
    if argv and argv[0] in list_providers():
        print(
            "warning: `apkd <provider> <package>` is deprecated; use `apkd download "
            "--providers <provider>` instead",
            file=sys.stderr,
        )
        args = _legacy_parser().parse_args(argv)
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.WARNING, format="%(message)s"
        )
        return _run(_request_from(args), [args.provider], args.output)

    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="%(message)s",
    )

    if args.command == "providers":
        for name, caps in list_providers().items():
            flags = ",".join(k for k, v in caps.items() if v)
            print(f"{name}: {flags or 'resolve-only'}")
        return EXIT_OK

    if args.command is None:
        parser.print_help()
        return EXIT_USAGE

    providers = [p.strip().lower() for p in args.providers.split(",") if p.strip()]
    unknown = [p for p in providers if p not in list_providers()]
    if unknown:
        print(f"error: unknown provider(s): {', '.join(unknown)}", file=sys.stderr)
        return EXIT_USAGE
    return _run(_request_from(args), providers, args.output)
