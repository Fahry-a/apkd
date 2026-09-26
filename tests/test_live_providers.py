import os
import tempfile
import unittest
from pathlib import Path

from apkd.models import DownloadRequest
from apkd.providers.apkcombo import APKComboProvider
from apkd.providers.apkmirror import APKMirrorProvider
from apkd.providers.apkpure import APKPureProvider
from apkd.providers.aptoide import AptoideProvider
from apkd.validation import validate_package


def _apkmirror_kwargs():
    # APKMirror uses org/repo slugs, not package names. Allow override via env:
    # APKD_APKMIRROR_SLUGS="com.pkg=org/repo,com.other=o2/r2"
    slug_map: dict[str, tuple[str, str]] = {}
    raw = os.getenv("APKD_APKMIRROR_SLUGS", "")
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        pkg, slug = chunk.split("=", 1)
        if "/" in slug:
            org, repo = slug.strip().split("/", 1)
            slug_map[pkg.strip()] = (org.strip(), repo.strip())
    # Sensible default for the CI default package.
    slug_map.setdefault("com.google.android.apps.photos", ("google-inc", "photos"))
    return {"slug_map": slug_map}


# These tests intentionally exercise public provider endpoints, so they are
# skipped unless APKD_LIVE_TESTS=1. Versions are supplied by CI environment
# variables so the workflow can pin an exact version after verifying it exists
# on the provider.
CASES = {
    "apkcombo": (APKComboProvider, os.getenv("APKD_APKCOMBO_PACKAGE", "com.google.android.apps.photos"), os.getenv("APKD_APKCOMBO_VERSION"), {}),
    "aptoide": (AptoideProvider, os.getenv("APKD_APTOIDE_PACKAGE", "com.google.android.apps.photos"), os.getenv("APKD_APTOIDE_VERSION"), {}),
    "apkpure": (APKPureProvider, os.getenv("APKD_APKPURE_PACKAGE", "com.google.android.apps.photos"), os.getenv("APKD_APKPURE_VERSION"), {}),
    "apkmirror": (APKMirrorProvider, os.getenv("APKD_APKMIRROR_PACKAGE", "com.google.android.apps.photos"), os.getenv("APKD_APKMIRROR_VERSION"), _apkmirror_kwargs()),
}


@unittest.skipUnless(
    os.getenv("APKD_LIVE_TESTS", "").strip().lower() in {"1", "true", "yes", "on"},
    "set APKD_LIVE_TESTS=1 to run tests that hit public provider endpoints",
)
class LiveProviderTests(unittest.TestCase):
    def test_each_provider_resolves_and_downloads(self):
        selected = os.getenv("APKD_PROVIDER")
        cases = {selected: CASES[selected]} if selected in CASES else CASES
        for name, (provider_cls, package, version, kwargs) in cases.items():
            with self.subTest(provider=name):
                provider = provider_cls(**kwargs)
                artifact = provider.resolve_request(DownloadRequest(package=package, version=version))
                self.assertEqual(artifact.package, package)
                self.assertTrue(artifact.url.startswith(("http://", "https://")))
                with tempfile.TemporaryDirectory() as tmp:
                    output = Path(tmp) / f"{name}{artifact.extension}"
                    provider.download(artifact, output)
                    validate_package(
                        output,
                        expected_extension=artifact.extension,
                        expected_sha256=(artifact.extra or {}).get("sha256"),
                    )


if __name__ == "__main__":
    unittest.main()
