import tempfile
import unittest
import zipfile
from pathlib import Path

from apkd.fallback import download
from apkd.models import Artifact, DownloadError, DownloadRequest, ProviderError
from apkd.providers import PROVIDER_ORDER, get_provider, list_providers
from apkd.providers.base import normalize_arch


def _make_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", "<manifest/>")
        z.writestr("classes.dex", "x" * 5000)


class FakeProvider:
    name = "fake"
    capabilities = {"version": True, "arch": True, "dpi": False, "min_sdk": False}

    def __init__(self, artifact=None, error=None, payload_bytes=None):
        from apkd.http import HttpClient
        self.http = HttpClient()
        self._artifact = artifact
        self._error = error
        self._payload = payload_bytes
        self.download_calls = 0

    def resolve_request(self, request):
        if self._error:
            raise self._error
        return self._artifact

    def download(self, artifact, destination):
        self.download_calls += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self._payload is not None:
            destination.write_bytes(self._payload)
            return len(self._payload)
        # default: write a valid zip
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("AndroidManifest.xml", "<manifest/>")
            z.writestr("classes.dex", "x" * 5000)
        destination.write_bytes(buf.getvalue())
        return len(buf.getvalue())


class FallbackTests(unittest.TestCase):
    def test_provider_registry_includes_opt_in_uptodown(self):
        self.assertEqual(
            set(list_providers()),
            {"apkcombo", "aptoide", "apkpure", "apkmirror", "uptodown"},
        )
        self.assertEqual(len(PROVIDER_ORDER), 4)
        self.assertNotIn("uptodown", PROVIDER_ORDER)

    def test_normalize_arch(self):
        self.assertEqual(normalize_arch("arm-v7a"), "armeabi-v7a")
        self.assertEqual(normalize_arch("arm64"), "arm64-v8a")
        self.assertIsNone(normalize_arch(None))

    def test_download_falls_back_to_second_provider(self):
        import apkd.fallback as fb
        req = DownloadRequest(package="com.example.app", version="1.0")
        good_artifact = Artifact("good", "com.example.app", "1.0", "https://example.com/a.apk", ".apk", "arm64-v8a")
        bad = FakeProvider(error=ProviderError("nope", provider="bad"))
        good = FakeProvider(artifact=good_artifact)
        orig = fb.get_provider
        fb.get_provider = lambda name: {"bad": bad, "good": good}[name]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "app.apk"
                result = download(req, providers=["bad", "good"], output=out)
                self.assertEqual(result.provider, "good")
                self.assertTrue(out.is_file())
        finally:
            fb.get_provider = orig

    def test_download_raises_aggregated_error(self):
        import apkd.fallback as fb
        req = DownloadRequest(package="com.example.app")
        bad1 = FakeProvider(error=ProviderError("fail1", provider="bad1"))
        bad2 = FakeProvider(error=ProviderError("fail2", provider="bad2"))
        orig = fb.get_provider
        fb.get_provider = lambda name: {"bad1": bad1, "bad2": bad2}[name]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(DownloadError) as ctx:
                    download(req, providers=["bad1", "bad2"], output=Path(tmp) / "a.apk")
                self.assertEqual(set(ctx.exception.provider_errors), {"bad1", "bad2"})
        finally:
            fb.get_provider = orig

    def test_invalid_download_is_skipped(self):
        import apkd.fallback as fb
        req = DownloadRequest(package="com.example.app", version="1.0")
        corrupt = Artifact("corrupt", "com.example.app", "1.0", "https://example.com/a.apk", ".apk")
        good_artifact = Artifact("good", "com.example.app", "1.0", "https://example.com/b.apk", ".apk")
        bad = FakeProvider(artifact=corrupt, payload_bytes=b"not a zip")
        good = FakeProvider(artifact=good_artifact)
        orig = fb.get_provider
        fb.get_provider = lambda name: {"bad": bad, "good": good}[name]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = download(req, providers=["bad", "good"], output=Path(tmp) / "a.apk")
                self.assertEqual(result.provider, "good")
        finally:
            fb.get_provider = orig

    def test_get_provider_unknown(self):
        with self.assertRaises(ValueError):
            get_provider("nosuch")

    def test_apkcombo_find_app_handles_redirect_and_relative_links(self):
        from apkd.providers.apkcombo import APKComboProvider

        class Resp:
            def __init__(self, url, text=""):
                self.url = url
                self.text = text

        # Case 1: exact search redirects straight to the app page.
        provider = APKComboProvider()
        provider.http.get = lambda url: Resp("https://apkcombo.com/google-photos/com.google.android.apps.photos/")
        self.assertEqual(
            provider._find_app("com.google.android.apps.photos"),
            "https://apkcombo.com/google-photos/com.google.android.apps.photos",
        )

        # Case 2: search results page with a relative href (no domain in href).
        provider = APKComboProvider()
        html = '<a href="/google-photos/com.google.android.apps.photos/">Google Photos</a>'
        provider.http.get = lambda url: Resp("https://apkcombo.com/search?q=com.google.android.apps.photos", html)
        self.assertEqual(
            provider._find_app("com.google.android.apps.photos"),
            "https://apkcombo.com/google-photos/com.google.android.apps.photos",
        )

    def test_apkmirror_extract_versions_and_variants(self):
        from apkd.providers import apkmirror as m

        versions_html = (
            '<div class="listWidget"><a name="all_versions"></a>'
            "<div>header</div>"
            '<div><div class="table-cell">x</div>'
            '<div class="table-cell"><a href="/apk/o/r/r-7-93-release/">7.93</a></div></div>'
            '<div><div class="table-cell">x</div>'
            '<div class=\"table-cell\"><a href="/apk/o/r/r-7-92-release/">7.92 beta</a></div></div>'
            "<div>more</div></div>"
        )
        versions = m.extract_versions(versions_html)
        self.assertEqual([v["name"] for v in versions], ["7.93", "7.92 beta"])
        self.assertTrue(versions[0]["url"].startswith("https://www.apkmirror.com/apk/o/r/"))

        variants_html = (
            '<div class="variants-table"><div>header</div>'
            '<div><div class="table-cell"><a href="/apk/o/r/v1">7.93</a><span>APK</span></div>'
            '<div class="table-cell">arm64-v8a</div>'
            '<div class="table-cell">9.0+</div>'
            '<div class="table-cell">nodpi</div></div>'
            '<div><div class="table-cell"><a href="/apk/o/r/v2">7.93</a><span>BUNDLE</span></div>'
            '<div class="table-cell">universal</div>'
            '<div class="table-cell">9.0+</div>'
            '<div class="table-cell">nodpi</div></div>'
            "</div>"
        )
        variants = m.extract_variants(variants_html)
        self.assertEqual(len(variants), 2)
        picked = m.filter_variant(variants, arch="arm64-v8a", dpi="nodpi",
                                  min_android=None, kind="apk")
        self.assertEqual(picked["url"], "https://www.apkmirror.com/apk/o/r/v1")
        # arch fallback to universal when the requested ABI is absent
        picked = m.filter_variant(variants, arch="x86", dpi="nodpi",
                                  min_android=None, kind="bundle")
        self.assertEqual(picked["url"], "https://www.apkmirror.com/apk/o/r/v2")

        dl_html = '<a class="downloadButton" href="/apk/o/r/dl1">dl</a>'
        self.assertEqual(m.extract_redirect_download_url(dl_html),
                         "https://www.apkmirror.com/apk/o/r/dl1")
        final_html = '<div class="card-with-tabs"><a href="https://dl.example/f.apk">f</a></div>'
        self.assertEqual(m.extract_final_download_url(final_html), "https://dl.example/f.apk")
        self.assertEqual(m.make_variants_url("o", "r", "7.93.0"), m.make_variants_url("o", "r", "7.93.0"))
        self.assertIn("7-93-0", m.make_variants_url("o", "r", "7.93.0"))
        self.assertEqual(m.extension_for("https://dl.example/f.apk", "apk"), ".apk")

    def test_apkmirror_resolve_request_end_to_end_mocked(self):
        from apkd.models import DownloadRequest
        from apkd.providers.apkmirror import APKMirrorProvider

        pages = {
            "repo": (
                '<div class="listWidget"><a name="all_versions"></a><div>h</div>'
                '<div><div class="table-cell">x</div>'
                '<div class="table-cell"><a href="/apk/o/r/r-1-0-release/">1.0</a></div></div>'
                "<div>m</div></div>"
            ),
            "variants": (
                '<div class="variants-table"><div>h</div>'
                '<div><div class="table-cell"><a href="/apk/o/r/d1">1.0</a><span>APK</span></div>'
                '<div class="table-cell">universal</div>'
                '<div class="table-cell">9.0+</div>'
                '<div class="table-cell">nodpi</div></div></div>'
            ),
            "dl": '<a class="downloadButton" href="/apk/o/r/redir">dl</a>',
            "redir": '<div class="card-with-tabs"><a href="https://dl.example/app.apk">f</a></div>',
        }

        class Resp:
            history = []

            def __init__(self, url, text):
                self.url = url
                self.text = text

        provider = APKMirrorProvider(slug_map={"com.example.app": ("o", "r")})

        def fake_fetch(url):
            if url == "https://www.apkmirror.com/apk/o/r":
                return pages["repo"], url, False
            if url == "https://www.apkmirror.com/apk/o/r/r-1-0-release/":
                return pages["variants"], url, False
            if url == "https://www.apkmirror.com/apk/o/r/d1":
                return pages["dl"], url, False
            if url == "https://www.apkmirror.com/apk/o/r/redir":
                return pages["redir"], url, False
            raise AssertionError(f"unexpected url {url}")

        provider._fetch = fake_fetch
        artifact = provider.resolve_request(DownloadRequest(package="com.example.app"))
        self.assertEqual(artifact.version, "1.0")
        self.assertEqual(artifact.url, "https://dl.example/app.apk")
        self.assertEqual(artifact.extension, ".apk")

    def test_apkmirror_prefix_from_version_entry(self):
        from apkd.providers import apkmirror as m

        self.assertEqual(
            m.prefix_from_version_entry(
                "https://www.apkmirror.com/apk/x-corp/twitter/x-12-28-0-prod-01-release/",
                "X 12.28.0-prod.01",
            ),
            "x-",
        )
        self.assertEqual(
            m.prefix_from_version_entry(
                "https://www.apkmirror.com/apk/admtorrent/advanced-download-manager/"
                "advanced-download-manager-14-0-39-release/",
                "Advanced Download Manager 14.0.39",
            ),
            "advanced-download-manager-",
        )
        self.assertEqual(
            m.prefix_from_version_entry(
                "https://www.apkmirror.com/apk/google-inc/photos/"
                "google-photos-7-92-0-977185651-release/",
                "Google Photos 7.92.0.977185651",
            ),
            "google-photos-",
        )
        self.assertIsNone(
            m.prefix_from_version_entry(
                "https://www.apkmirror.com/apk/o/r/something-else/",
                "1.0",
            )
        )

    def test_apkmirror_discover_variants_url_placeholder_removed(self):
        # _discover_variants_url was a single-result wrapper kept only for
        # backward compatibility. Assert the real method is the entry point.
        from apkd.providers.apkmirror import APKMirrorProvider

        self.assertFalse(hasattr(APKMirrorProvider, "_discover_variants_url"))
        self.assertTrue(hasattr(APKMirrorProvider, "_candidate_variants_urls"))

    def test_apkmirror_discover_variants_url(self):
        from apkd.providers.apkmirror import APKMirrorProvider

        repo_html = (
            '<div class="listWidget"><a name="all_versions"></a><div>h</div>'
            '<div><div class="table-cell">x</div><div class="table-cell">'
            '<a href="/apk/x-corp/twitter/x-12-28-0-prod-01-release/">X 12.28.0-prod.01</a>'
            "</div></div><div>m</div></div>"
        )
        provider = APKMirrorProvider(slug_map={"com.twitter.android": ("x-corp", "twitter")})
        provider._get_text = lambda url: repo_html

        # Exact hit on the listing is used verbatim.
        self.assertEqual(
            provider._candidate_variants_urls("x-corp", "twitter", "12.28.0-prod.01"),
            ["https://www.apkmirror.com/apk/x-corp/twitter/x-12-28-0-prod-01-release/"],
        )
        # Older version absent from the listing reuses the derived display prefix.
        self.assertEqual(
            provider._candidate_variants_urls("x-corp", "twitter", "12.19.1-release.0")[0],
            "https://www.apkmirror.com/apk/x-corp/twitter/x-12-19-1-release-0-release/",
        )

    def test_apkmirror_reports_cloudflare_challenge_explicitly(self):
        from apkd.providers.apkmirror import APKMirrorProvider

        provider = APKMirrorProvider()

        def blocked(url, **kwargs):
            del url, kwargs
            raise RuntimeError("403 Client Error: Forbidden")

        provider.http.get = blocked
        with self.assertRaisesRegex(Exception, "Cloudflare/Turnstile"):
            provider._fetch("https://www.apkmirror.com/example")

    def test_apkmirror_discover_variants_url_falls_back(self):
        from apkd.providers.apkmirror import APKMirrorProvider, make_variants_url

        provider = APKMirrorProvider(slug_map={"com.example.app": ("o", "r")})

        def boom(url):
            raise ValueError("offline")

        provider._get_text = boom
        self.assertEqual(
            provider._candidate_variants_urls("o", "r", "1.0")[-1],
            make_variants_url("o", "r", "1.0"),
        )

    def test_apkmirror_candidate_urls_cover_truncated_prefix(self):
        from apkd.providers.apkmirror import APKMirrorProvider

        provider = APKMirrorProvider(
            slug_map={"com.pinterest": (
                "pinterest", "pinterest-one-destination-for-a-world-of-inspiration")})

        def boom(url):
            raise ValueError("offline")

        provider._get_text = boom
        candidates = provider._candidate_variants_urls(
            "pinterest", "pinterest-one-destination-for-a-world-of-inspiration",
            "14.34.0")
        self.assertIn(
            "https://www.apkmirror.com/apk/pinterest/"
            "pinterest-one-destination-for-a-world-of-inspiration/"
            "pinterest-one-destination-for-a-world-of-inspiration-14-34-0-release/",
            candidates,
        )
        # First dash-token of a long repo slug is tried for truncated
        # display prefixes such as `pinterest-14-34-0-release/`.
        self.assertIn(
            "https://www.apkmirror.com/apk/pinterest/"
            "pinterest-one-destination-for-a-world-of-inspiration/"
            "pinterest-14-34-0-release/",
            candidates,
        )

    def test_apkmirror_apk_request_falls_back_to_bundle_variant(self):
        from apkd.models import DownloadRequest
        from apkd.providers.apkmirror import APKMirrorProvider

        pages = {
            "repo": (
                '<div class="listWidget"><a name="all_versions"></a><div>h</div>'
                '<div><div class="table-cell">x</div>'
                '<div class="table-cell"><a href="/apk/o/r/r-2-0-release/">2.0</a></div></div>'
                "<div>m</div></div>"
            ),
            "variants": (
                '<div class="variants-table"><div>h</div>'
                '<div><div class="table-cell"><a href="/apk/o/r/d1">2.0</a><span>BUNDLE</span></div>'
                '<div class="table-cell">universal</div>'
                '<div class="table-cell">9.0+</div>'
                '<div class="table-cell">nodpi</div></div></div>'
            ),
            "dl": '<a class="downloadButton" href="/apk/o/r/redir">dl</a>',
            "redir": '<div class="card-with-tabs"><a href="https://dl.example/app.apkm">f</a></div>',
        }

        class Resp:
            history = []

            def __init__(self, url, text):
                self.url = url
                self.text = text

        provider = APKMirrorProvider(slug_map={"com.example.app": ("o", "r")})

        def fake_fetch(url):
            if url == "https://www.apkmirror.com/apk/o/r":
                return pages["repo"], url, False
            if url == "https://www.apkmirror.com/apk/o/r/r-2-0-release/":
                return pages["variants"], url, False
            if url == "https://www.apkmirror.com/apk/o/r/d1":
                return pages["dl"], url, False
            if url == "https://www.apkmirror.com/apk/o/r/redir":
                return pages["redir"], url, False
            raise AssertionError(f"unexpected url {url}")

        provider._fetch = fake_fetch
        artifact = provider.resolve_request(
            DownloadRequest(package="com.example.app", version="2.0"))
        self.assertEqual(artifact.version, "2.0")
        self.assertEqual(artifact.url, "https://dl.example/app.apkm")
        self.assertEqual(artifact.extension, ".apkm")
        self.assertEqual(artifact.extra.get("variant_type"), "bundle")

    def test_validation_accepts_bundles_with_apk_entries(self):
        import tempfile
        import zipfile
        from pathlib import Path

        from apkd.validation import validate_package

        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "app.apkm"
            with zipfile.ZipFile(bundle, "w") as z:
                z.writestr("base.apk", "x" * 5000)
            validate_package(bundle, expected_extension=".apkm")

            apk = Path(tmp) / "app.apk"
            with zipfile.ZipFile(apk, "w") as z:
                z.writestr("base.apk", "x" * 5000)
            with self.assertRaises(ValueError):
                validate_package(apk)

            junk = Path(tmp) / "junk.apkm"
            with zipfile.ZipFile(junk, "w") as z:
                z.writestr("readme.txt", "x" * 5000)
            with self.assertRaises(ValueError):
                validate_package(junk)

    def test_validation_enforces_universal_native_abis(self):
        import tempfile
        import zipfile
        from pathlib import Path

        from apkd.validation import validate_package

        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "app.apk"
            with zipfile.ZipFile(apk, "w") as archive:
                archive.writestr("AndroidManifest.xml", b"x")
                archive.writestr("classes.dex", b"x" * 5000)
                archive.writestr("lib/arm64-v8a/libapp.so", b"arm64")
            with self.assertRaisesRegex(ValueError, "armeabi-v7a"):
                validate_package(apk, expected_arch="universal")

            with zipfile.ZipFile(apk, "a") as archive:
                archive.writestr("lib/armeabi-v7a/libapp.so", b"arm32")
            validate_package(apk, expected_arch="universal")

    def test_validation_unions_bundle_native_abis(self):
        import tempfile
        import zipfile
        from pathlib import Path

        from apkd.validation import validate_package

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.apk"
            split64 = root / "split64.apk"
            split32 = root / "split32.apk"
            for path, abi in ((base, None), (split64, "arm64-v8a"),
                              (split32, "armeabi-v7a")):
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("AndroidManifest.xml", b"x")
                    archive.writestr("classes.dex", b"x" * 5000)
                    if abi:
                        archive.writestr(f"lib/{abi}/libapp.so", b"native")
            bundle = root / "app.apkm"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.write(base, "base.apk")
                archive.write(split64, "split_config.arm64_v8a.apk")
                archive.write(split32, "split_config.armeabi_v7a.apk")
            validate_package(bundle, expected_arch="universal")

    def test_validation_verifies_provider_sha256(self):
        import hashlib
        import io

        from apkd.validation import validate_package

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"<manifest/>")
            archive.writestr("classes.dex", b"x" * 5000)
        payload = buffer.getvalue()
        digest = hashlib.sha256(payload).hexdigest()

        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "app.apk"
            good.write_bytes(payload)
            validate_package(good, expected_sha256=digest)
            validate_package(good, expected_sha256=digest.upper())

            with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
                validate_package(good, expected_sha256="0" * 64)
            with self.assertRaisesRegex(ValueError, "unusable sha256"):
                validate_package(good, expected_sha256="not-a-hash")

    def test_validation_caps_bundle_inspection(self):
        from apkd.validation import MAX_BUNDLE_ENTRIES, validate_package

        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bomb.apkm"
            with zipfile.ZipFile(bundle, "w") as archive:
                for index in range(MAX_BUNDLE_ENTRIES + 1):
                    archive.writestr(f"split{index}.apk", b"x" * 5000)
            with self.assertRaisesRegex(ValueError, "more than"):
                validate_package(bundle)

    def test_output_path_cannot_escape_the_working_directory(self):
        from apkd.fallback import _coerce_output

        artifact = Artifact(
            provider="fake", package="com.example.app", version="1.0",
            url="https://example.invalid/a.apk", extension=".apk",
        )
        hostile = DownloadRequest(package="../../../etc/cron.d/evil", version="1.0")
        dest = _coerce_output(hostile, artifact, None)
        self.assertEqual(dest.parent, Path("."))
        self.assertNotIn("..", dest.name)

        hostile_version = DownloadRequest(package="com.example.app", version="../../x")
        dest = _coerce_output(hostile_version, artifact, None)
        self.assertEqual(dest.parent, Path("."))
        self.assertNotIn("..", dest.name)

    def test_apkcombo_resolves_variant_file_behind_interstitial(self):
        from apkd.providers.apkcombo import APKComboProvider

        html = (
            '<div class="content-tab" id="best-variant-tab">'
            '<a href="/r2?u=https%3A%2F%2Fcdn.example%2Funi.apk" class="variant">DL</a>'
            "</div>"
            '<div class="content-tab" id="variants-tab">'
            '<li>arm64-v8a <a href="/r2?u=https%3A%2F%2Fcdn.example%2Farm64.apk" class="variant">DL</a></li>'
            "</div>"
        )

        class Resp:
            url = "https://apkcombo.com/x/download/apk"
            headers = {"content-type": "text/html"}
            text = html

        for arch, expected in (
            (None, "https://apkcombo.com/r2?u=https%3A%2F%2Fcdn.example%2Funi.apk"),
            ("arm64-v8a", "https://apkcombo.com/r2?u=https%3A%2F%2Fcdn.example%2Farm64.apk"),
        ):
            provider = APKComboProvider()
            provider.http.get = lambda url, **kw: Resp()
            self.assertEqual(
                provider._resolve_variant_url("https://apkcombo.com/x/download/apk", arch),
                expected,
            )


if __name__ == "__main__":
    unittest.main()
