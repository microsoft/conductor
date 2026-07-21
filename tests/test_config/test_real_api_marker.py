"""Regression tests for the ``real_api`` marker auto-skip hook (issue #326).

Without ``tests/conftest.py::pytest_collection_modifyitems``, nothing
deselects ``@pytest.mark.real_api`` tests by default. A plain ``pytest``,
``pytest -m "not performance"``, or ``make test`` would run them, spawning
real Copilot/Claude subprocesses that can collide with (and kill) a live
``conductor run --web-bg`` session.

These tests use pytest's built-in ``pytester`` fixture to run an isolated
inner pytest session whose sandbox ``conftest.py`` delegates to the *actual*
``pytest_collection_modifyitems`` hook defined in ``tests/conftest.py``
(loaded by file path, not copy-pasted), so this suite fails if that hook is
weakened, removed, or its ``-m`` detection regressed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT_CONFTEST = (Path(__file__).parent.parent / "conftest.py").resolve()

# Delegates to the real hook under test via importlib, rather than
# duplicating its logic, so this suite tracks tests/conftest.py verbatim.
_SANDBOX_CONFTEST = f'''
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "conductor_root_conftest_under_test", r"{_ROOT_CONFTEST}"
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

pytest_collection_modifyitems = _module.pytest_collection_modifyitems
'''

_SANDBOX_TEST_MODULE = """
import pytest


def test_regular():
    assert True


@pytest.mark.real_api
def test_marked_real_api():
    assert True
"""


@pytest.fixture
def real_api_sandbox(pytester: pytest.Pytester) -> pytest.Pytester:
    """A pytester sandbox wired to the real conftest.py hook under test."""
    pytester.makeconftest(_SANDBOX_CONFTEST)
    pytester.makepyfile(test_sandbox=_SANDBOX_TEST_MODULE)
    return pytester


def test_real_api_skipped_by_default(real_api_sandbox: pytest.Pytester) -> None:
    """No ``-m`` expression: the real_api test must be skipped, not executed."""
    result = real_api_sandbox.runpytest()
    result.assert_outcomes(passed=1, skipped=1)


def test_real_api_skipped_with_not_performance(real_api_sandbox: pytest.Pytester) -> None:
    """Reproduces the issue's exact repro: ``-m "not performance"`` must still skip it."""
    result = real_api_sandbox.runpytest("-m", "not performance")
    result.assert_outcomes(passed=1, skipped=1)


def test_real_api_runs_when_explicitly_selected(real_api_sandbox: pytest.Pytester) -> None:
    """``-m real_api`` opts in: the test must run (not be skipped by our hook)."""
    result = real_api_sandbox.runpytest("-m", "real_api")
    result.assert_outcomes(passed=1, deselected=1)


def test_ci_marker_expression_still_deselects(real_api_sandbox: pytest.Pytester) -> None:
    """CI's exact ``-m "not real_api and not performance"`` must keep deselecting it."""
    result = real_api_sandbox.runpytest("-m", "not real_api and not performance")
    result.assert_outcomes(passed=1, deselected=1)


def test_suite_would_catch_a_reverted_hook(real_api_sandbox: pytest.Pytester) -> None:
    """Load-bearing check: a no-op hook must NOT skip the real_api test.

    Proves the ``skipped=1`` assertions above are meaningful (i.e. this
    suite would fail if the real hook were reverted/broken) rather than
    trivially passing regardless of hook behavior.
    """
    real_api_sandbox.makeconftest(
        """
def pytest_collection_modifyitems(config, items):
    pass
"""
    )
    result = real_api_sandbox.runpytest()
    result.assert_outcomes(passed=2)
