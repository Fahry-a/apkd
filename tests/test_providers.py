import unittest

from apkd.providers.apkcombo import APKComboProvider
from apkd.providers.aptoide import AptoideProvider
from apkd.providers.uptodown import UptodownProvider


class ProviderUnitTests(unittest.TestCase):
    def test_aptoide_search_requires_exact_package(self):
        provider = AptoideProvider()
        data = {
            "datalist": {"list": [
                {"package": "wrong.package", "file": {"vername": "1"}},
                {"package": "com.example.app", "file": {"vername": "2"}},
            ]}
        }
        provider._get_json = lambda path, params: data
        self.assertEqual(provider.resolve("com.example.app").version, "2")

    def test_apkcombo_version_normalization(self):
        self.assertTrue(APKComboProvider._same_version("1.2.3 (123)", "1.2.3"))
        self.assertFalse(APKComboProvider._same_version("1.2.3", "1.2.4"))

    def test_uptodown_version_normalization(self):
        self.assertEqual(UptodownProvider._normalize("7.90 [123]"), "7.90")


if __name__ == "__main__":
    unittest.main()
