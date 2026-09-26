import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apkd.models import Artifact, DownloadRequest, ProviderError
from apkd.providers.apkcombo import APKComboProvider
from apkd.providers.apkpure import APKPureProvider
from apkd.providers.aptoide import AptoideProvider
from apkd.providers.uptodown import UptodownProvider, UptodownTarget


class _StubClient:
    """Stands in for the signed APKPure client."""

    def app_his_version(self, cfg, package):
        # The live endpoint returns an empty catalog for several apps.
        return {"retcode": 0, "errmsg": "success", "version_list": []}

    def app_detail(self, cfg, package, use_cache=False):
        return {"retcode": 0, "app_detail": {"version_name": "1.4.2"}}

    def extract_asset(self, response):
        detail = response.get("app_detail") or response
        return {
            "version_name": detail.get("version_name"),
            "asset_type": None,
            "asset_url": None,
        }


class _Page:
    status_code = 200

    def __init__(self, text, url="https://apkpure.com/page"):
        self.text = text
        self.url = url


class _Redirecting(_Page):
    def __init__(self, final_url, text):
        super().__init__(text, url=final_url)


class ProviderUnitTests(unittest.TestCase):
    def test_aptoide_search_requires_exact_package(self):
        provider = AptoideProvider()
        data = {
            "datalist": {"list": [
                {"package": "wrong.package", "file": {"vername": "1", "path": "https://example.com/a.apk"}},
                {"package": "com.example.app", "file": {"vername": "2", "path": "https://example.com/b.apk"}},
            ]}
        }
        provider._get_json = lambda path, params: data
        self.assertEqual(provider.resolve("com.example.app").version, "2")

    def test_aptoide_hydrates_url_from_getapp(self):
        provider = AptoideProvider()
        provider._find_version = lambda package, version: {
            "file": {"vername": version}
        }
        provider._get_app_file = lambda package, version: {
            "vername": version,
            "path": "https://cdn.example/app.apk",
        }
        artifact = provider.resolve("com.example.app", "1.2.3")
        self.assertEqual(artifact.url, "https://cdn.example/app.apk")
        self.assertEqual(artifact.version, "1.2.3")

    def test_aptoide_does_not_hydrate_a_different_version(self):
        provider = AptoideProvider()
        provider._post_json = lambda path, payload: {
            "nodes": {"meta": {"data": {"file": {
                "vername": "9.9.9", "path": "https://example.test/new.apk"
            }}}}
        }
        self.assertIsNone(provider._get_app_file("com.example.app", "1.2.3"))

    def test_apkpure_web_finds_version_when_history_api_is_empty(self):
        """The signed history endpoint returns an empty catalog for some apps.

        Regression: the exact old release was then unreachable, because the
        public version page was only reachable through a configured slug hint
        and a search-page scrape that returns no result links.
        """
        provider = APKPureProvider()
        provider._client = lambda: (_StubClient(), None)

        detail = (
            '<a href="https://d.apkpure.com/b/XAPK/com.rawcam.app'
            '?versionCode=52&nc=armeabi-v7a&sv=26">Download XAPK</a>'
            '<span>Architecture armeabi-v7a</span>'
        )
        listing = (
            '<a href="https://apkpure.com/native-camera/com.rawcam.app/download/1.4.1">'
            "Native Camera 1.4.1 XAPK</a>"
        )
        pages = {
            "https://apkpure.com/_/com.rawcam.app": _Redirecting(
                "https://apkpure.com/native-camera/com.rawcam.app", listing
            ),
            "https://apkpure.com/native-camera/com.rawcam.app/versions": _Page(listing),
            "https://apkpure.com/native-camera/com.rawcam.app/download/1.4.1": _Page(detail),
        }
        provider._web_get = lambda url: pages.get(url)

        artifact = provider.resolve_request(
            DownloadRequest(package="com.rawcam.app", version="1.4.1", arch="arm32")
        )
        self.assertEqual(artifact.version, "1.4.1")
        self.assertEqual(artifact.extension, ".xapk")
        self.assertIn("nc=armeabi-v7a", artifact.url)
        # The arm32 request must not be served the arm64 asset.
        self.assertNotIn("nc=arm64-v8a", artifact.url)

    def test_apkpure_web_refuses_to_relabel_split_assets_as_universal(self):
        provider = APKPureProvider()
        provider._client = lambda: (_StubClient(), None)
        detail = "".join(
            f'<a href="https://d.apkpure.com/b/XAPK/com.rawcam.app'
            f'?versionCode=52&nc={abi}">Download XAPK</a>'
            f"<span>Architecture {abi}</span>"
            for abi in ("arm64-v8a", "armeabi-v7a")
        )
        base = "https://apkpure.com/native-camera/com.rawcam.app"
        pages = {
            "https://apkpure.com/_/com.rawcam.app": _Redirecting(base, ""),
            f"{base}/versions": _Page(""),
            f"{base}/download/1.4.1": _Page(detail),
        }
        provider._web_get = lambda url: pages.get(url)

        with self.assertRaisesRegex(ProviderError, "only separate"):
            provider.resolve_request(
                DownloadRequest(package="com.rawcam.app", version="1.4.1", arch="universal")
            )

    def test_apkpure_history_walk_is_depth_bounded(self):
        node: dict = {}
        cursor = node
        for _ in range(200):
            child: dict = {}
            cursor["nested"] = child
            cursor = child
        cursor["version_name"] = "9.9.9"
        # Must return rather than exhaust the interpreter's recursion budget.
        self.assertIsNone(APKPureProvider._search_history(node, "9.9.9"))

    def test_apkcombo_resolves_opaque_d_variant_without_downloading(self):
        from apkd.providers.apkcombo import APKComboProvider

        html = (
            '<a href="https://apkcombo.com/d?u=opaque-token" class="variant">'
            "Advanced Download Manager XAPK</a>"
        )

        class Resp:
            url = "https://apkcombo.com/x/download/phone-1-0-apk"
            headers = {"content-type": "text/html"}
            text = html

        provider = APKComboProvider()
        provider.http.get = lambda url, **kw: Resp()
        self.assertEqual(
            provider._resolve_variant_url(Resp.url, None),
            "https://apkcombo.com/d?u=opaque-token",
        )

    def test_apkcombo_preserves_r2_guard_parameters(self):
        from apkd.providers.apkcombo import APKComboProvider

        href = (
            "/r2?u=https%3A%2F%2Fcdn.example%2Fapp.apk"
            "&fp=abc&ip=127.0.0.1&package_name=com.example.app&lang=en"
        )
        html = f'<a href="{href}" class="variant">APK</a>'

        class Resp:
            url = "https://apkcombo.com/example/com.example.app/download/1.0-apk"
            headers = {"content-type": "text/html"}
            text = html

        provider = APKComboProvider()
        provider.http.get = lambda url, **kw: Resp()
        self.assertEqual(
            provider._resolve_variant_url(Resp.url, None),
            "https://apkcombo.com" + href,
        )

    # Live-shaped "All variants" panel for Pinterest 14.34.0: the plain
    # /download/{id} page serves Uptodown's store wrapper, the real artifacts
    # are the -x variant pages (first row is the universal APK).
    _UPTODOWN_PANEL = """
    <section class="variants"><div class="content">
    <p>arm64-v8a, armeabi-v7a, x86, x86_64</p>
    <div class="variant">
      <div class="v-version" onclick="location.href='https://pinterest.en.uptodown.com/android/download/1210795434-x';">14.34.0</div>
      <div class="v-file"><span class="apk">apk</span></div>
    </div>
    <p>arm64-v8a, armeabi-v7a, x86_64</p>
    <div class="variant">
      <div class="v-version" onclick="location.href='https://pinterest.en.uptodown.com/android/download/1210814246-x';">14.34.0</div>
      <div class="v-file"><span class="xapk">xapk</span></div>
    </div>
    <p>arm64-v8a, armeabi-v7a</p>
    <div class="variant">
      <div class="v-version" onclick="location.href='https://pinterest.en.uptodown.com/android/download/1213582223-x';">14.34.0</div>
      <div class="v-file"><span class="xapk">xapk</span></div>
    </div>
    </div></section>
    """

    def test_uptodown_resolves_exact_metadata_without_browser(self):
        class Response:
            def __init__(self, url, text="", payload=None):
                self.url = url
                self.text = text
                self._payload = payload or {}

            def json(self):
                return self._payload

        app_html = (
            '<h1 id="detail-app-name" data-code="20013">Pinterest</h1>'
            '<span>Package Name: com.pinterest</span>'
        )
        versions = {
            "success": 1,
            "data": [
                {
                    "fileID": 1213582223,
                    "version": "14.34.0",
                    "kindFile": "xapk",
                    "versionURL": {
                        "url": "https://pinterest.en.uptodown.com/android",
                        "extraURL": "download",
                        "versionID": 1213582223,
                    },
                },
                {
                    "fileID": 1210795434,
                    "version": "14.34.0",
                    "kindFile": "apk",
                    "versionURL": {
                        "url": "https://pinterest.en.uptodown.com/android",
                        "extraURL": "download",
                        "versionID": 1210795434,
                    },
                },
            ],
        }

        def get(url, **kwargs):
            del kwargs
            if url.endswith("/android"):
                return Response(url, app_html)
            if "/apps/20013/versions/" in url:
                return Response(url, payload=versions)
            if "/download/" in url:
                return Response(
                    url,
                    '<button class="button variants" '
                    'data-version="13986103">All variants</button>',
                )
            if "/app/20013/version/13986103/files" in url:
                return Response(url, payload={"content": self._UPTODOWN_PANEL})
            raise AssertionError(url)

        provider = UptodownProvider()
        provider.http.get = get
        target = provider.resolve_target(
            DownloadRequest(
                package="com.pinterest", version="14.34.0",
                arch="universal", prefer_xapk=True, app_slug="pinterest",
            )
        )
        self.assertEqual(target.file_id, "1213582223")
        self.assertEqual(target.kind, "xapk")
        self.assertEqual(target.page_url.rsplit("/", 1)[-1], "1213582223-x")

    def test_uptodown_uses_configured_app_id_without_app_page(self):
        calls = []

        class Response:
            text = (
                '<button class="button variants" '
                'data-version="13986103">All variants</button>'
            )

            def __init__(self, url=None, payload=None):
                self.url = (
                    url or "https://pinterest.en.uptodown.com/android"
                    "/apps/20013/versions/1"
                )
                self._payload = payload if payload is not None else {
                    "data": [{
                        "fileID": 1213582223,
                        "version": "14.34.0",
                        "kindFile": "xapk",
                        "versionURL": {
                            "url": "https://pinterest.en.uptodown.com/android",
                            "extraURL": "download",
                            "versionID": 1213582223,
                        },
                    }]
                }

            def json(self):
                return self._payload

        panel = self._UPTODOWN_PANEL

        def get(url, **kwargs):
            del kwargs
            calls.append(url)
            if "/app/20013/version/13986103/files" in url:
                return Response(payload={"content": panel})
            return Response()

        provider = UptodownProvider()
        provider.http.get = get
        target = provider.resolve_target(
            DownloadRequest(
                package="com.pinterest", version="14.34.0", arch="universal",
                prefer_xapk=True, app_slug="pinterest", app_id="20013",
            )
        )
        self.assertEqual(target.app_id, "20013")
        self.assertEqual(target.page_url.rsplit("/", 1)[-1], "1213582223-x")
        self.assertEqual(
            calls,
            ["https://pinterest.en.uptodown.com/android/apps/20013/versions/1",
             "https://pinterest.en.uptodown.com/android/download/1213582223",
             "https://pinterest.en.uptodown.com/app/20013/version/13986103/files"],
        )

    def test_uptodown_selects_universal_apk_variant_page(self):
        """The plain download page is the store wrapper; the -x variant is real.

        Regression for the manual finding that /download/1210795434 serves
        ``com.uptodown`` while /download/1210795434-x serves Pinterest 14.34.0
        (universal APK: arm64-v8a, armeabi-v7a, x86, x86_64).
        """
        class Response:
            def __init__(self, url, text="", payload=None):
                self.url = url
                self.text = text
                self._payload = payload or {}

            def json(self):
                return self._payload

        versions = {"data": [{
            "fileID": 1210795434,
            "version": "14.34.0",
            "kindFile": "apk",
            "versionURL": {
                "url": "https://pinterest.en.uptodown.com/android",
                "extraURL": "download",
                "versionID": 1210795434,
            },
        }]}

        def get(url, **kwargs):
            del kwargs
            if "/apps/20013/versions/" in url:
                return Response(url, payload=versions)
            if "/download/" in url:
                return Response(
                    url,
                    '<button class="button variants" '
                    'data-version="13986103">All variants</button>',
                )
            if "/app/20013/version/13986103/files" in url:
                return Response(url, payload={"content": self._UPTODOWN_PANEL})
            raise AssertionError(url)

        provider = UptodownProvider()
        provider.http.get = get
        target = provider.resolve_target(
            DownloadRequest(
                package="com.pinterest", version="14.34.0",
                arch="universal", app_slug="pinterest", app_id="20013",
            )
        )
        self.assertEqual(target.file_id, "1210795434")
        self.assertEqual(target.kind, "apk")
        self.assertTrue(target.page_url.endswith("/download/1210795434-x"))

    def test_uptodown_keeps_plain_page_without_variants_button(self):
        """Single-variant apps have no variants button; keep old behavior."""
        class Response:
            def __init__(self, url, text="", payload=None):
                self.url = url
                self.text = text
                self._payload = payload or {}

            def json(self):
                return self._payload

        versions = {"data": [{
            "fileID": 999,
            "version": "1.0",
            "kindFile": "apk",
            "versionURL": {
                "url": "https://example.en.uptodown.com/android",
                "extraURL": "download",
                "versionID": 999,
            },
        }]}

        def get(url, **kwargs):
            del kwargs
            if "/apps/1/versions/" in url:
                return Response(url, payload=versions)
            return Response(url, "<html>no variants button here</html>")

        provider = UptodownProvider()
        provider.http.get = get
        target = provider.resolve_target(
            DownloadRequest(
                package="com.example", version="1.0",
                arch="universal", app_slug="example", app_id="1",
            )
        )
        self.assertEqual(target.file_id, "999")
        self.assertTrue(target.page_url.endswith("/download/999"))

    def test_uptodown_requires_visible_browser_for_download(self):
        class Response:
            url = "https://pinterest.en.uptodown.com/android"
            text = (
                '<h1 id="detail-app-name" data-code="20013">Pinterest</h1>'
                '<span>Package Name: com.pinterest</span>'
            )

            def json(self):
                return {"data": [{
                    "fileID": 1213582223,
                    "version": "14.34.0",
                    "kindFile": "xapk",
                    "versionURL": {
                        "url": "https://pinterest.en.uptodown.com/android",
                        "extraURL": "download",
                        "versionID": 1213582223,
                    },
                }]}

        def get(url, **kwargs):
            del kwargs
            if url.endswith("/android"):
                return Response()
            return Response()

        provider = UptodownProvider(browser=False)
        provider.http.get = get
        with self.assertRaisesRegex(ProviderError, "interactive browser"):
            provider.resolve_request(
                DownloadRequest(
                    package="com.pinterest", version="14.34.0",
                    arch="universal", prefer_xapk=True, app_slug="pinterest",
                )
            )

    def test_uptodown_extracts_browser_download_url(self):
        self.assertEqual(
            UptodownProvider._download_url_from_payload({
                "data": {"downloadURL": "https://dw.uptodown.com/dwn/file"}
            }),
            "https://dw.uptodown.com/dwn/file",
        )
        self.assertEqual(
            UptodownProvider._download_url_from_payload({
                "data": {"downloadURL": "file-token"}
            }),
            "https://dw.uptodown.com/dwn/file-token",
        )

    def test_uptodown_reports_server_error_from_browser_payload(self):
        with self.assertRaisesRegex(ProviderError, "challenge rejected"):
            UptodownProvider._download_url_from_payload({
                "success": 0,
                "errorMsg": "challenge rejected",
            })

    def test_uptodown_cdp_endpoint_enables_browser_mode(self):
        provider = UptodownProvider(cdp_url="http://127.0.0.1:9222")
        self.assertTrue(provider._browser_enabled())

    def test_uptodown_invisible_mode_is_opt_in(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APKD_UPTODOWN_INVISIBLE", None)
            os.environ.pop("APKD_UPTODOWN_SEED", None)
            self.assertFalse(UptodownProvider()._invisible_enabled())
            self.assertIsNone(UptodownProvider()._invisible_seed())
        with patch.dict(os.environ, {"APKD_UPTODOWN_INVISIBLE": "1"}):
            self.assertTrue(UptodownProvider()._invisible_enabled())
        with patch.dict(os.environ, {"APKD_UPTODOWN_SEED": "42"}):
            self.assertEqual(UptodownProvider()._invisible_seed(), 42)
        with patch.dict(os.environ, {"APKD_UPTODOWN_SEED": "nope"}):
            self.assertIsNone(UptodownProvider()._invisible_seed())
        # Explicit constructor flags win over the environment.
        with patch.dict(os.environ, {"APKD_UPTODOWN_INVISIBLE": "1"}):
            self.assertFalse(UptodownProvider(invisible=False)._invisible_enabled())
        self.assertEqual(UptodownProvider(invisible_seed=7)._invisible_seed(), 7)

    def test_uptodown_invisible_mode_needs_the_package(self):
        target = UptodownTarget(
            app_url="https://pinterest.en.uptodown.com/android",
            app_id="20013", version="14.34.0", file_id="1210795434",
            kind="apk",
            page_url="https://pinterest.en.uptodown.com/android/download/1210795434-x",
            only_xapk="0",
        )
        provider = UptodownProvider(browser=True, invisible=True)
        with patch.dict(sys.modules, {"invisible_playwright": None}):
            with self.assertRaisesRegex(ProviderError, "invisible-playwright"):
                provider._browser_download_url(target)

    def test_uptodown_headless_retry_settings_are_bounded(self):
        provider = UptodownProvider(
            headless=True,
            initial_wait=5,
            retry_wait=5,
            max_attempts=2,
            response_timeout=30,
        )
        self.assertTrue(provider._headless_enabled())
        self.assertEqual(provider._initial_wait_seconds(), 5)
        self.assertEqual(provider._retry_wait_seconds(), 5)
        self.assertEqual(provider._max_attempts(), 2)
        self.assertEqual(provider._response_timeout_ms(), 30000)

    def test_uptodown_download_uses_captured_browser_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "captured.apk"
            destination = Path(directory) / "output.apk"
            source.write_bytes(b"browser-captured-package")
            artifact = Artifact(
                "uptodown", "com.example", "1.0", "https://example.test/file",
                ".apk", extra={"browser_download_path": str(source)},
            )
            written = UptodownProvider().download(artifact, destination)
            self.assertEqual(written, len(b"browser-captured-package"))
            self.assertEqual(destination.read_bytes(), b"browser-captured-package")
            self.assertFalse(source.exists())

    def test_uptodown_reports_http_error_payload(self):
        message = UptodownProvider._http_error_message(
            400,
            "https://pinterest.en.uptodown.com/ajax/app/20013/file/1213582223/download-url",
            {"success": 0, "errorCode": -51, "errorMsg": "Bad Request"},
            "",
        )
        self.assertIn("HTTP 400", message)
        self.assertIn("errorCode=-51", message)
        self.assertIn("errorMsg=Bad Request", message)

    def test_uptodown_rejects_button_metadata_mismatch(self):
        class Button:
            def get_attribute(self, name):
                return {"data-app-id": "20013", "data-file-id": "wrong"}.get(name)

        with self.assertRaisesRegex(ProviderError, "does not match"):
            UptodownProvider._validate_button_metadata(
                Button(),
                type("Target", (), {"app_id": "20013", "file_id": "1213582223"})(),
            )

    def test_uptodown_redacts_token_in_raw_response(self):
        redacted = UptodownProvider._redact_response_text(
            '{"token":"secret-value","errorMsg":"Bad Request"}'
        )
        self.assertNotIn("secret-value", redacted)
        self.assertIn("<redacted>", redacted)

    def test_apkpure_web_fallback_selects_exact_bundle_asset(self):
        html = (
            '<a href="https://d.apkpure.com/b/XAPK/com.example.app?versionCode=52&'
            'nc=arm64-v8a&sv=26">XAPK</a>'
        )

        class Response:
            status_code = 200
            text = html
            url = "https://apkpure.com/example/com.example.app/download/1.2.3"

        provider = APKPureProvider()
        provider._web_get = lambda url: Response()
        info = provider._find_web_version(
            DownloadRequest(
                package="com.example.app", version="1.2.3",
                app_slug="example", prefer_xapk=True, arch="arm64-v8a",
            )
        )
        self.assertEqual(info["version_name"], "1.2.3")
        self.assertEqual(info["asset_type"], "XAPK")
        self.assertIn("d.apkpure.com/b/XAPK", info["asset_url"])

    def test_apkpure_web_fallback_accepts_apkm_container(self):
        html = (
            '<a href="https://d.apkpure.com/b/APKM/com.example.app?versionCode=52&'
            'nc=arm64-v8a%2Carmeabi-v7a">APKM</a>'
        )

        class Response:
            status_code = 200
            text = html
            url = "https://apkpure.com/example/com.example.app/download/1.2.3"

        provider = APKPureProvider()
        provider._web_get = lambda url: Response()
        info = provider._find_web_version(
            DownloadRequest(
                package="com.example.app", version="1.2.3",
                app_slug="example", prefer_xapk=True, arch="universal",
            )
        )
        self.assertEqual(info["asset_type"], "APKM")

    def test_apkpure_web_fallback_rejects_split_only_universal_assets(self):
        html = (
            '<a href="https://d.apkpure.com/b/XAPK/com.example.app?nc=armeabi-v7a">'
            "arm32</a>"
            '<a href="https://d.apkpure.com/b/XAPK/com.example.app?nc=arm64-v8a">'
            "arm64</a>"
        )

        class Response:
            status_code = 200
            text = html
            url = "https://apkpure.com/example/com.example.app/download/1.2.3"

        provider = APKPureProvider()
        provider._web_get = lambda url: Response()
        with self.assertRaisesRegex(ProviderError, "separate arm64-v8a/armeabi-v7a"):
            provider._find_web_version(
                DownloadRequest(
                    package="com.example.app", version="1.2.3",
                    app_slug="example", prefer_xapk=True, arch="universal",
                )
            )

    def test_apkcombo_version_normalization(self):
        self.assertTrue(APKComboProvider._same_version("1.2.3 (123)", "1.2.3"))
        self.assertFalse(APKComboProvider._same_version("1.2.3", "1.2.4"))

    def test_apkcombo_url_contains_exact_version(self):
        from apkd.providers.apkcombo import _url_contains_version as contains

        self.assertTrue(contains(
            "https://apkcombo.com/native-camera/com.rawcam.app/download/phone-1-4-1-apk",
            "1.4.1",
        ))
        self.assertTrue(contains(
            "https://apkcombo.com/twitter/com.twitter.android/download/phone-12-19-1-release-0-apk",
            "12.19.1-release.0",
        ))
        self.assertFalse(contains(
            "https://apkcombo.com/native-camera/com.rawcam.app/download/phone-1-4-2-apk",
            "1.4.1",
        ))
        self.assertFalse(contains(
            "https://apkcombo.com/native-camera/com.rawcam.app/download/phone-1-4-2-apk",
            "1.4",
        ))

    def test_apkcombo_does_not_use_stale_version_candidate(self):
        from bs4 import BeautifulSoup

        from apkd.providers.apkcombo import APKComboProvider

        class Response:
            def __init__(self, url, text):
                self.url = url
                self.text = text
                self.headers = {"content-type": "text/html"}

        split_page = (
            '<li>arm64-v8a <a class="variant" href="/d?u=arm64">DL</a></li>'
        )
        stale_page = '<a class="variant" href="/d?u=universal">universal</a>'
        page_html = (
            '<a href="/x/download/phone-1-4-1-apk">1.4.1</a>'
            '<a href="/x/download/phone-1-4-apk">1.4</a>'
        )

        def get(url, **kwargs):
            if "1-4-1" in url:
                return Response(url, split_page)
            if "1-4" in url:
                return Response(url, stale_page)
            raise AssertionError(url)

        provider = APKComboProvider()
        provider.http.get = get
        with self.assertRaises(Exception):
            provider._download_url(
                BeautifulSoup(page_html, "html.parser"),
                "https://apkcombo.com/x/old-versions/",
                "universal",
                expected_version="1.4.1",
            )

    def test_apkcombo_text_contains_version(self):
        from apkd.providers.apkcombo import _text_contains_version as contains

        self.assertTrue(contains("Native Camera 1.4.1 XAPK Sep 10, 2026", "1.4.1"))
        self.assertFalse(contains("Native Camera 1.4.10 XAPK Sep 10, 2026", "1.4.1"))
        self.assertTrue(contains("Pinterest 14.34.0 APK Apr 1, 2026", "14.34.0"))
        self.assertFalse(contains("Pinterest 14.14.0 APK Apr 15, 2026", "14.34.0"))
        self.assertFalse(contains(None, "1.4.1"))
        self.assertFalse(contains("Native Camera 1.4.1", None))

    def test_apkcombo_find_version_link_matches_labeled_old_versions(self):
        from bs4 import BeautifulSoup

        from apkd.providers.apkcombo import APKComboProvider

        html = (
            '<a href="/native-camera/com.rawcam.app/download/phone-1-4-2-xapk/">'
            "Native Camera 1.4.2 XAPK Sep 17, 2026 \u00b7 Android 8.0+</a>"
            '<a href="/native-camera/com.rawcam.app/download/phone-1-4-1-xapk/">'
            "Native Camera 1.4.1 XAPK Sep 10, 2026 \u00b7 Android 8.0+</a>"
        )
        provider = APKComboProvider()
        self.assertEqual(
            provider._find_version_link(BeautifulSoup(html, "html.parser"), "1.4.1"),
            "/native-camera/com.rawcam.app/download/phone-1-4-1-xapk/",
        )
        self.assertIsNone(
            provider._find_version_link(BeautifulSoup(html, "html.parser"), "9.9.9")
        )


if __name__ == "__main__":
    unittest.main()
