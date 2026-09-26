"""Unit tests for the shadow-root unsealing.

The bug this module exists for is specific and worth pinning: under patchright
none of playwright-captcha's own injection routes actually run, and an ordinary
``page.evaluate`` writes to an isolated world the page never sees. These tests
use fakes to check the two things that matters — the main world is targeted
explicitly, and the patched library lookups keep the original's waiting
behaviour instead of looking once and giving up.
"""

import asyncio
import unittest
from unittest import mock

from apkd.providers import _shadow_unlock as su


class FakeElement:
    def __init__(self, src=""):
        self._src = src
        self.frame = FakeFrame()

    async def get_attribute(self, name):
        return self._src if name == "src" else None

    async def content_frame(self):
        return self.frame


class FakeFrame:
    def __init__(self, detached=False):
        self._detached = detached

    def is_detached(self):
        return self._detached

    async def query_selector_all(self, selector):
        return []

    async def wait_for_timeout(self, ms):
        return None


class FakeQueryable:
    """A page/frame stand-in whose iframe list can be made to appear late."""

    def __init__(self, batches):
        self._batches = list(batches)
        self.calls = 0
        self.waits = []

    async def query_selector_all(self, selector):
        self.calls += 1
        index = min(self.calls - 1, len(self._batches) - 1)
        return self._batches[index]

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)


class MainWorldTests(unittest.TestCase):
    def test_isolated_context_is_forced_off_when_supported(self):
        """patchright needs this; without it the patch lands out of sight."""
        page = mock.Mock()
        page.evaluate = mock.AsyncMock(return_value="ok")

        asyncio.run(su._evaluate_main_world(page, "expr"))

        page.evaluate.assert_awaited_once_with("expr", isolated_context=False)

    def test_falls_back_when_driver_has_no_such_parameter(self):
        calls = []

        async def evaluate(expression, *args, **kwargs):
            calls.append((expression, kwargs))
            if "isolated_context" in kwargs:
                raise TypeError("unexpected keyword argument")
            return "ok"

        page = mock.Mock()
        page.evaluate = evaluate

        result = asyncio.run(su._evaluate_main_world(page, "expr"))

        self.assertEqual(result, "ok")
        # First attempt forces the main world, the retry drops the keyword for
        # drivers that do not accept it.
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], {"isolated_context": False})
        self.assertEqual(calls[1][1], {})

    def test_is_patched_reads_the_flag(self):
        page = mock.Mock()
        page.evaluate = mock.AsyncMock(return_value=True)
        self.assertTrue(asyncio.run(su.is_patched(page)))

    def test_is_patched_is_false_when_the_check_errors(self):
        page = mock.Mock()
        page.evaluate = mock.AsyncMock(side_effect=RuntimeError("detached"))
        self.assertFalse(asyncio.run(su.is_patched(page)))


class PlainLookupTests(unittest.TestCase):
    def test_iframe_search_waits_for_the_widget_to_mount(self):
        """A single lookup returns nothing; the widget arrives a second later."""
        cf = FakeElement(src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x")
        queryable = FakeQueryable(batches=[[], [], [cf]])

        with mock.patch.object(su, "IFRAME_WAIT_SECONDS", 5.0):
            frames = asyncio.run(
                su._plain_iframe_frames(
                    mock.Mock(), queryable, "https://challenges.cloudflare.com/"
                )
            )

        self.assertEqual(len(frames), 1)
        self.assertGreaterEqual(queryable.calls, 3)
        self.assertTrue(queryable.waits, "never waited between attempts")

    def test_iframe_search_gives_up_and_returns_nothing(self):
        """An iframe that never appears must return empty, not hang or raise."""
        queryable = FakeQueryable(batches=[[]])
        with mock.patch.object(su, "IFRAME_WAIT_SECONDS", 0.05):
            frames = asyncio.run(
                su._plain_iframe_frames(mock.Mock(), queryable, "nope")
            )
        self.assertEqual(frames, [])
        self.assertGreater(queryable.calls, 1, "did not retry at all")

    def test_detached_frames_are_dropped(self):
        detached = FakeElement(src="https://challenges.cloudflare.com/x")
        detached.frame = FakeFrame(detached=True)
        queryable = FakeQueryable(batches=[[detached]])

        with mock.patch.object(su, "IFRAME_WAIT_SECONDS", 0.05):
            frames = asyncio.run(
                su._plain_iframe_frames(
                    mock.Mock(), queryable, "challenges.cloudflare.com"
                )
            )
        self.assertEqual(frames, [])

    def test_non_matching_iframes_are_ignored(self):
        other = FakeElement(src="https://ads.example.com/frame")
        queryable = FakeQueryable(batches=[[other]])

        with mock.patch.object(su, "IFRAME_WAIT_SECONDS", 0.05):
            frames = asyncio.run(
                su._plain_iframe_frames(
                    mock.Mock(), queryable, "https://challenges.cloudflare.com/"
                )
            )
        self.assertEqual(frames, [])

    def test_element_search_returns_immediately_when_present(self):
        found = [object()]
        queryable = FakeQueryable(batches=[found])

        result = asyncio.run(su._plain_elements(mock.Mock(), queryable, "input"))

        self.assertEqual(result, found)
        self.assertEqual(queryable.calls, 1)
        self.assertFalse(queryable.waits)

    def test_element_search_polls_until_the_timeout(self):
        found = [object()]
        # Each batch is the complete result for that attempt.
        queryable = FakeQueryable(batches=[[], [], found])

        result = asyncio.run(
            su._plain_elements(mock.Mock(), queryable, "input", timeout=5)
        )

        self.assertEqual(result, found)
        self.assertGreaterEqual(queryable.calls, 3)

    def test_element_search_survives_an_erroring_page(self):
        class Broken:
            def __init__(self):
                self.calls = 0

            async def query_selector_all(self, selector):
                self.calls += 1
                raise RuntimeError("detached")

            async def wait_for_timeout(self, ms):
                return None

        broken = Broken()
        result = asyncio.run(
            su._plain_elements(mock.Mock(), broken, "input", timeout=0.05)
        )
        self.assertEqual(result, [])
        self.assertGreater(broken.calls, 1)


class ShadowLookupsPatchTests(unittest.TestCase):
    def test_patch_is_installed_and_always_restored(self):
        from playwright_captcha.solvers.click.cloudflare import solve_by_click
        from playwright_captcha.solvers.click.cloudflare.utils import dom_helpers

        original_iframes = solve_by_click.search_shadow_root_iframes
        original_elements = dom_helpers.search_shadow_root_elements

        with su.shadow_lookups():
            self.assertIs(solve_by_click.search_shadow_root_iframes, su._plain_iframe_frames)
            self.assertIs(dom_helpers.search_shadow_root_elements, su._plain_elements)
        self.assertIs(solve_by_click.search_shadow_root_iframes, original_iframes)
        self.assertIs(dom_helpers.search_shadow_root_elements, original_elements)

    def test_patch_is_restored_after_an_error(self):
        from playwright_captcha.solvers.click.cloudflare import solve_by_click

        original = solve_by_click.search_shadow_root_iframes
        with self.assertRaises(RuntimeError):
            with su.shadow_lookups():
                raise RuntimeError("boom")
        self.assertIs(solve_by_click.search_shadow_root_iframes, original)

    def test_disabled_leaves_the_library_untouched(self):
        from playwright_captcha.solvers.click.cloudflare import solve_by_click

        original = solve_by_click.search_shadow_root_iframes
        with su.shadow_lookups(enabled=False) as applied:
            self.assertFalse(applied)
        self.assertIs(solve_by_click.search_shadow_root_iframes, original)


if __name__ == "__main__":
    unittest.main()
