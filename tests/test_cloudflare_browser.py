"""Unit tests for the opt-in Cloudflare browser solve used by APKMirror.

The real browser is never launched here: the solver is injected so the tests
only cover the wiring around it (when it triggers, what it does with the
clearance cookie, and that it is used at most once per provider instance).
"""

import unittest
from unittest import mock

from apkd.models import ProviderError
from apkd.providers import _cloudflare
from apkd.providers import apkmirror
from apkd.providers.apkmirror import CHALLENGE_MARKER, APKMirrorProvider


CHALLENGE_HTML = (
    '<html><head><title>Just a moment...</title></head>'
    f'<body>{CHALLENGE_MARKER}</body></html>'
)
GOOD_HTML = '<html><body><a name="all_versions"></a></body></html>'


class FakeResponse:
    def __init__(self, text, url="https://www.apkmirror.com/example", history=()):
        self.text = text
        self.url = url
        self.history = list(history)


class FakeClearance:
    def __init__(self, *, cleared=True, cookies=None, user_agent="UA/1.0"):
        self.challenge_cleared = cleared
        self.cookies = cookies if cookies is not None else [
            {"name": "cf_clearance", "value": "abc", "domain": ".apkmirror.com", "path": "/"}
        ]
        self.user_agent = user_agent
        self.final_url = "https://www.apkmirror.com/example"
        self.applied = False

    def to_session(self, http):
        self.applied = True
        for cookie in self.cookies:
            http.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
            )


class CloudflareSolverTests(unittest.TestCase):
    @staticmethod
    def _no_impersonation():
        """Keep the curl_cffi stage out of the way so no real request is made.

        Without this, a challenged fake response falls through to a live
        curl_cffi call against apkmirror.com, which would make these unit tests
        depend on the network (and could silently pass).
        """
        return mock.patch.object(apkmirror, "_HAS_IMPERSONATION", False)

    def test_browser_solve_is_opt_in(self):
        self.assertFalse(APKMirrorProvider()._browser_solve_enabled())

    def test_browser_solve_enabled_by_argument(self):
        self.assertTrue(APKMirrorProvider(browser_solve=True)._browser_solve_enabled())

    def test_browser_solve_enabled_by_env(self):
        with mock.patch.dict("os.environ", {"APKD_APKMIRROR_BROWSER": "1"}):
            self.assertTrue(APKMirrorProvider()._browser_solve_enabled())

    def test_headless_is_opt_in(self):
        self.assertFalse(APKMirrorProvider()._browser_headless_enabled())
        with mock.patch.dict("os.environ", {"APKD_CF_HEADLESS": "1"}):
            self.assertTrue(APKMirrorProvider()._browser_headless_enabled())

    def test_challenge_solved_once_then_reuses_clearance(self):
        provider = APKMirrorProvider(browser_solve=True)
        clearance = FakeClearance()

        bodies = [FakeResponse(CHALLENGE_HTML), FakeResponse(GOOD_HTML)]
        provider.http.get = lambda url, **kw: bodies.pop(0)

        with self._no_impersonation(), mock.patch.object(
            apkmirror, "solve_cloudflare_challenge", return_value=clearance
        ) as solve:
            text, url, redirected = provider._fetch("https://www.apkmirror.com/example")

        self.assertEqual(text, GOOD_HTML)
        self.assertFalse(redirected)
        self.assertTrue(clearance.applied)
        solve.assert_called_once()
        # The clearance cookie is now on the shared session.
        self.assertEqual(
            provider.http.session.cookies.get("cf_clearance", domain=".apkmirror.com"),
            "abc",
        )

    def test_uncleared_browser_run_raises_actionable_error(self):
        provider = APKMirrorProvider(browser_solve=True)
        provider.http.get = lambda url, **kw: FakeResponse(CHALLENGE_HTML)

        with self._no_impersonation(), mock.patch.object(
            apkmirror,
            "solve_cloudflare_challenge",
            return_value=FakeClearance(cleared=False, cookies=[]),
        ):
            with self.assertRaisesRegex(ProviderError, "patchright"):
                provider._fetch("https://www.apkmirror.com/example")

    def test_retry_after_solve_still_challenged_raises(self):
        provider = APKMirrorProvider(browser_solve=True)
        provider.http.get = lambda url, **kw: FakeResponse(CHALLENGE_HTML)

        with self._no_impersonation(), mock.patch.object(
            apkmirror, "solve_cloudflare_challenge", return_value=FakeClearance()
        ):
            with self.assertRaisesRegex(ProviderError, "Cloudflare/Turnstile"):
                provider._fetch("https://www.apkmirror.com/example")

    def test_browser_launched_only_once_per_provider(self):
        """A clearance that did not stick must not relaunch a browser per page."""
        provider = APKMirrorProvider(browser_solve=True)
        provider.http.get = lambda url, **kw: FakeResponse(CHALLENGE_HTML)

        with self._no_impersonation(), mock.patch.object(
            apkmirror, "solve_cloudflare_challenge", return_value=FakeClearance()
        ) as solve:
            with self.assertRaises(ProviderError):
                provider._fetch("https://www.apkmirror.com/a")
            with self.assertRaisesRegex(ProviderError, "already ran"):
                provider._fetch("https://www.apkmirror.com/b")

        self.assertEqual(solve.call_count, 1)

    def test_refused_request_is_not_retried_with_impersonation(self):
        """Stage 2 only applies to a challenged body, not a refused request."""
        provider = APKMirrorProvider()

        def blocked(url, **kwargs):
            raise RuntimeError("403 Client Error: Forbidden")

        provider.http.get = blocked
        with mock.patch("apkd.providers.apkmirror._impersonated_requests") as imp:
            with self.assertRaisesRegex(ProviderError, "Cloudflare/Turnstile"):
                provider._fetch("https://www.apkmirror.com/example")
        imp.get.assert_not_called()

    def test_disabled_browser_keeps_the_original_error_message(self):
        provider = APKMirrorProvider()
        provider.http.get = lambda url, **kw: FakeResponse(CHALLENGE_HTML)

        with mock.patch("apkd.providers.apkmirror._impersonated_requests") as imp:
            imp.get.return_value = FakeResponse(CHALLENGE_HTML, url="https://x")
            with self.assertRaisesRegex(ProviderError, "no automatic bypass|APKD_APKMIRROR_BROWSER"):
                provider._fetch("https://www.apkmirror.com/example")

    def test_successful_page_never_starts_a_browser(self):
        provider = APKMirrorProvider(browser_solve=True)
        provider.http.get = lambda url, **kw: FakeResponse(GOOD_HTML)

        with mock.patch.object(apkmirror, "solve_cloudflare_challenge") as solve:
            text, _, _ = provider._fetch("https://www.apkmirror.com/example")

        self.assertEqual(text, GOOD_HTML)
        solve.assert_not_called()


class FrameworkSelectionTests(unittest.TestCase):
    def test_stealth_backend_is_preferred_on_chromium(self):
        with mock.patch.object(
            _cloudflare, "available_frameworks",
            return_value=["patchright", "playwright"],
        ):
            self.assertEqual(
                _cloudflare.resolve_framework(browser="chromium"), "patchright"
            )

    def test_firefox_cannot_use_the_cdp_only_stealth_patch(self):
        """patchright injects over CDP, which Firefox does not have.

        Silently falling back would run unpatched and lose the very stealth the
        backend was chosen for, so it must be reported instead.
        """
        with mock.patch.object(
            _cloudflare, "available_frameworks",
            return_value=["patchright", "playwright"],
        ):
            self.assertEqual(
                _cloudflare.resolve_framework(browser="firefox"), "playwright"
            )
            with self.assertRaisesRegex(ProviderError, "Chromium-only"):
                _cloudflare.resolve_framework("patchright", browser="firefox")

    def test_explicit_backend_is_honoured(self):
        with mock.patch.object(
            _cloudflare, "available_frameworks",
            return_value=["patchright", "playwright"],
        ):
            self.assertEqual(
                _cloudflare.resolve_framework("playwright", browser="chromium"),
                "playwright",
            )

    def test_explicit_missing_backend_is_an_error_not_a_downgrade(self):
        with mock.patch.object(
            _cloudflare, "available_frameworks", return_value=["playwright"]
        ):
            with self.assertRaisesRegex(ProviderError, "not installed"):
                _cloudflare.resolve_framework("patchright", browser="chromium")

    def test_unknown_backend_is_rejected(self):
        with self.assertRaisesRegex(ProviderError, "unknown browser backend"):
            _cloudflare.resolve_framework("selenium", browser="chromium")

    def test_unknown_browser_is_rejected(self):
        with self.assertRaisesRegex(ProviderError, "unknown browser"):
            _cloudflare.resolve_framework(browser="lynx")

    def test_missing_backends_report_install_instructions(self):
        with mock.patch.object(_cloudflare, "available_frameworks", return_value=[]):
            with self.assertRaisesRegex(ProviderError, "playwright-captcha"):
                _cloudflare.resolve_framework()

    def test_clearance_replaces_user_agent(self):
        """cf_clearance is bound to the UA that earned it."""
        from apkd.http import HttpClient

        http = HttpClient()
        _cloudflare.BrowserClearance(
            cookies=[{"name": "cf_clearance", "value": "x", "domain": ".apkmirror.com"}],
            user_agent="BrowserUA/2.0",
        ).to_session(http)

        self.assertEqual(http.session.headers["User-Agent"], "BrowserUA/2.0")
        self.assertEqual(
            http.session.cookies.get("cf_clearance", domain=".apkmirror.com"), "x"
        )


if __name__ == "__main__":
    unittest.main()
