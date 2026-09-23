import os
import tempfile
import unittest
from pathlib import Path

from apkd.providers.apkcombo import APKComboProvider
from apkd.providers.aptoide import AptoideProvider
from apkd.providers.uptodown import UptodownProvider
from apkd.validation import validate_package


# These tests intentionally exercise public provider endpoints.
# Versions are supplied by CI environment variables so the workflow can pin
# an exact version after verifying it exists on the provider.
CASES = {
    "uptodown": (UptodownProvider, os.getenv("APKD_UPTODOWN_PACKAGE", "com.google.android.apps.photos"), os.getenv("APKD_UPTODOWN_VERSION")),
    "apkcombo": (APKComboProvider, os.getenv("APKD_APKCOMBO_PACKAGE", "com.google.android.apps.photos"), os.getenv("APKD_APKCOMBO_VERSION")),
    "aptoide": (AptoideProvider, os.getenv("APKD_APTOIDE_PACKAGE", "com.google.android.apps.photos"), os.getenv("APKD_APTOIDE_VERSION")),
}


class LiveProviderTests(unittest.TestCase):
    def test_each_provider_resolves_and_downloads(self):
        selected = os.getenv("APKD_PROVIDER")
        cases = {selected: CASES[selected]} if selected in CASES else CASES
        for name, (provider_cls, package, version) in cases.items():
            with self.subTest(provider=name):
                provider = provider_cls()
                artifact = provider.resolve(package, version)
                self.assertEqual(artifact.package, package)
                self.assertTrue(artifact.url.startswith(("http://", "https://")))
                with tempfile.TemporaryDirectory() as tmp:
                    output = Path(tmp) / f"{name}{artifact.extension}"
                    provider.http.download(artifact.url, output)
                    validate_package(output, expected_extension=artifact.extension)


if __name__ == "__main__":
    unittest.main()
