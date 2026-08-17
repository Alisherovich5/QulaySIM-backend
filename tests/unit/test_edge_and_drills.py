"""Two pieces that must be inert until somebody turns them on.

Both are written for a future state — an edge cache that does not exist yet, and
a drill nobody has run — and both live in the request path or the worker. So what
these tests hold down is not what they do, but that they do nothing at all while
unconfigured, and that they cannot raise.
"""

from __future__ import annotations

import pytest

from app.integrations import cloudflare

pytestmark = pytest.mark.anyio


class TestEdgePurgeIsInertUntilConfigured:
    async def test_it_reports_itself_unconfigured(self) -> None:
        assert cloudflare.is_configured() is False

    async def test_purging_does_nothing_and_says_so(self) -> None:
        """Zero accepted, no exception, no HTTP call.

        This runs from `invalidate_catalog`, which runs from a price sync. A
        purge that raised while unconfigured would take the sync down with it.
        """
        assert await cloudflare.purge(["https://qulaysim.uz/api/countries"]) == 0

    async def test_an_empty_list_is_not_a_request(self) -> None:
        assert await cloudflare.purge([]) == 0

    async def test_the_catalogue_helper_is_inert_too(self) -> None:
        assert await cloudflare.purge_catalogue(["turkey"]) == 0


class TestTheDrillTaskIsHarmless:
    """Time is patched out here, and the first version of this file is why.

    Without it the clamp test asked the task to hold a worker for its maximum and
    then actually waited: two minutes six seconds, on every CI run, to assert an
    integer. A test that sleeps is a test that gets skipped.
    """

    @pytest.fixture(autouse=True)
    def _no_waiting(self, monkeypatch):
        import app.workers.tasks.diagnostics as diagnostics

        monkeypatch.setattr(diagnostics.time, "sleep", lambda _seconds: None)

    def test_it_touches_nothing_and_reports_its_attempt(self) -> None:
        """The attempt number is the whole point of the drill: a second attempt
        with the same id proves the queue redelivered an interrupted job."""
        from app.workers.tasks.diagnostics import slow_noop

        result = slow_noop.apply(args=(1, "unit-test")).get()
        assert result["label"] == "unit-test"
        assert result["seconds"] == 1
        assert result["attempt"] == 0

    def test_the_duration_is_clamped(self) -> None:
        """A drill that could be asked to hold a worker for an hour would be a
        way to take the queue down rather than a way to test it."""
        from app.workers.tasks.diagnostics import slow_noop

        assert slow_noop.apply(args=(10_000, "clamp")).get()["seconds"] == 120
        assert slow_noop.apply(args=(0, "clamp")).get()["seconds"] == 1
