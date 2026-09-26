"""Humanised checkbox click for playwright-captcha's Cloudflare solver.

``playwright-captcha`` presses the checkbox with ``element.click()``, which
teleports the cursor to the element centre in a single tick. This module
replaces only that click step -- the library still does the finding and the
verifying -- with a short ``page.mouse`` approach: a few waypoints with
jitter, a randomised pause, an off-centre landing, then press and release.

Every input goes through ``page.mouse``, so the browser still synthesises the
trusted events itself; nothing here spoofs ``isTrusted``.

The previous implementation of this file modelled a full throw curve and
replayed recorded human strokes (818 lines across two modules). Its own tests
could only assert the model's invariants, never that Cloudflare was fooled, and
the recorded dataset was excluded from the distribution, so the replay half was
inert for every user but the author. What is kept here is the part that
actually changes the input the browser sees.
"""

from __future__ import annotations

import random
from contextlib import contextmanager
from typing import Any, Iterator

# Nominal travel time per pace. Each page.mouse.move() is a round trip to the
# browser (~16ms), so these are floors rather than targets.
PACE_PROFILES: dict[str, dict[str, float]] = {
    "flick": {"waypoints": 4, "travel_ms": 120, "settle_ms": 40},
    "quick": {"waypoints": 7, "travel_ms": 260, "settle_ms": 90},
    "careful": {"waypoints": 11, "travel_ms": 520, "settle_ms": 160},
}

# A cursor parked near the top-left is a strong tell in itself, so the resting
# point is offset from the origin rather than at 0,0.
_REST = (220.0, 180.0)


def _profile(pace: str) -> dict[str, float]:
    return PACE_PROFILES.get(pace, PACE_PROFILES["quick"])


class VirtualMouse:
    """Drives a real pointer to a target element, then clicks it."""

    def __init__(self, page: Any, rng: random.Random | None = None, pace: str = "quick") -> None:
        self.page = page
        self.rng = rng or random.Random()
        self.profile = _profile(pace)

    async def approach(self, box: dict[str, float]) -> tuple[float, float]:
        """Move to a point near the centre of ``box`` and return it."""
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        # Land slightly off-centre, the way a hand does.
        x += self.rng.uniform(-0.18, 0.18) * box["width"]
        y += self.rng.uniform(-0.22, 0.22) * box["height"]

        start = self.rng.uniform(0.6, 1.4) * _REST
        waypoints = max(2, int(self.profile["waypoints"]))
        for index in range(1, waypoints + 1):
            progress = index / waypoints
            # Ease-out so the pointer decelerates into the target.
            eased = 1 - (1 - progress) ** 2
            jitter = (1 - progress) * 6.0
            await self.page.mouse.move(
                start[0] + (x - start[0]) * eased + self.rng.uniform(-jitter, jitter),
                start[1] + (y - start[1]) * eased + self.rng.uniform(-jitter, jitter),
            )
        travel = self.profile["travel_ms"] / 1000
        await self.page.wait_for_timeout(travel * self.rng.uniform(0.75, 1.35))
        return x, y

    async def click(self, element: Any) -> None:
        box = await element.bounding_box()
        if not box:
            # Off-screen or not laid out: fall back to the library's own click.
            await element.click()
            return
        x, y = await self.approach(box)
        await self.page.mouse.down()
        await self.page.wait_for_timeout(
            self.profile["settle_ms"] * self.rng.uniform(0.7, 1.6)
        )
        await self.page.mouse.up()

    async def fidget(self, duration_ms: int) -> None:
        """Small idle movement, so the pointer is not perfectly still."""
        x, y = _REST
        steps = max(1, duration_ms // 120)
        for _ in range(steps):
            x += self.rng.uniform(-9, 9)
            y += self.rng.uniform(-9, 9)
            await self.page.mouse.move(x, y)
            await self.page.wait_for_timeout(self.rng.uniform(40, 130))


@contextmanager
def humanised_clicks(
    page: Any,
    enabled: bool = True,
    seed: int | None = None,
    pace: str = "quick",
    on_click: Any = None,
    fidget_ms: int = 2600,
) -> Iterator[bool]:
    """Route playwright-captcha's checkbox clicks through :class:`VirtualMouse`.

    Patches the one function the library uses to press the checkbox, leaving its
    detection and verification logic untouched. Restored on exit, including when
    the solver raises.

    ``on_click`` is called just before each press, so a caller can report
    progress when Cloudflare re-shows the box.
    """
    if not enabled:
        yield False
        return

    from playwright_captcha.solvers.click.cloudflare import solve_by_click

    original = solve_by_click.click_checkbox
    mouse = VirtualMouse(page, random.Random(seed), pace=pace)

    async def replacement(checkbox: Any, attempts: int) -> None:
        if on_click is not None:
            on_click()
        await mouse.click(checkbox)
        await mouse.fidget(fidget_ms)

    solve_by_click.click_checkbox = replacement
    try:
        yield True
    finally:
        solve_by_click.click_checkbox = original
