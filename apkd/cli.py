from __future__ import annotations

import argparse
from pathlib import Path

from .models import ProviderError
from .providers import get_provider
from .validation import validate_package


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download Android APKs from mirror providers.")
    parser.add_argument("provider", choices=("uptodown", "apkcombo", "aptoide"))
    parser.add_argument("package", help="Android package name, e.g. com.example.app")
    parser.add_argument("--version", help="Exact version; omit to resolve the provider's latest version")
    parser.add_argument("--arch", help="Requested ABI where supported (e.g. armeabi-v7a, arm64-v8a)")
    parser.add_argument("-o", "--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    provider = get_provider(args.provider)
    artifact = provider.resolve(args.package, args.version, args.arch)
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() != artifact.extension:
        output = output.with_suffix(artifact.extension)
    size = provider.http.download(artifact.url, output)
    validate_package(output, expected_extension=artifact.extension)
    print(f"provider={artifact.provider} version={artifact.version} url={artifact.url}")
    print(f"file={output} size={size}")
    return 0
