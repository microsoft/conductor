"""Shared fixtures for skill tests."""

from __future__ import annotations

import pytest

from conductor.skills.loader import _cached_skill_payload


@pytest.fixture(autouse=True)
def _clear_skill_content_cache() -> None:
    """Drop the loader's per-directory content cache between tests.

    ``_cached_skill_payload`` is keyed on ``(directory, name)`` with no
    mtime component, so a test that rewrites a skill at a path an earlier
    test already loaded would silently receive the stale body and pass for
    the wrong reason. ``tmp_path`` makes that rare rather than impossible.
    """
    _cached_skill_payload.cache_clear()
