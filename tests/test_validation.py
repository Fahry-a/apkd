import tempfile
import unittest
import zipfile
from pathlib import Path

from apkd.validation import validate_package


def _write_zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)


class ValidationTests(unittest.TestCase):
    def test_apk_requires_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.apk"
            _write_zip(path, {"classes.dex": b"x" * 5000})
            with self.assertRaisesRegex(ValueError, "AndroidManifest"):
                validate_package(path, expected_extension=".apk")

    def test_xapk_accepts_embedded_apk_with_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "base.apk"
            _write_zip(nested, {"AndroidManifest.xml": b"manifest", "classes.dex": b"x" * 5000})
            path = Path(tmp) / "app.xapk"
            _write_zip(path, {"base.apk": nested.read_bytes(), "manifest.json": b"{}"})
            validate_package(path, expected_extension=".xapk")


if __name__ == "__main__":
    unittest.main()
