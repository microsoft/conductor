"""Regression tests for the ``install_scripts`` marker auto-skip hook (issue #331).

See ``tests/conftest.py::pytest_collection_modifyitems`` for the full
rationale behind the hook this suite exercises. Without this hook, a plain
``pytest`` / ``pytest -m "not performance"`` invocation (or CI's main test
job, whose ``-m`` expression never mentions ``install_scripts``) would run
the install-script E2E suite, which can SIGTERM-kill any live
``conductor run --web-bg`` process on the host.

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


@pytest.mark.install_scripts
def test_marked_install_scripts():
    assert True
"""


@pytest.fixture
def install_scripts_sandbox(pytester: pytest.Pytester) -> pytest.Pytester:
    """A pytester sandbox wired to the real conftest.py hook under test."""
    pytester.makeconftest(_SANDBOX_CONFTEST)
    pytester.makepyfile(test_sandbox=_SANDBOX_TEST_MODULE)
    return pytester


def test_install_scripts_skipped_by_default(
    install_scripts_sandbox: pytest.Pytester,
) -> None:
    """No ``-m`` expression: the install_scripts test must be skipped, not executed."""
    result = install_scripts_sandbox.runpytest()
    result.assert_outcomes(passed=1, skipped=1)


def test_install_scripts_skipped_with_not_performance(
    install_scripts_sandbox: pytest.Pytester,
) -> None:
    """Reproduces the issue's exact repro: ``-m "not performance"`` must still skip it."""
    result = install_scripts_sandbox.runpytest("-m", "not performance")
    result.assert_outcomes(passed=1, skipped=1)


def test_install_scripts_skipped_by_ci_main_job_expression(
    install_scripts_sandbox: pytest.Pytester,
) -> None:
    """CI's main test-job expression never mentions ``install_scripts``.

    ``.github/workflows/ci.yml`` / ``release.yml`` run
    ``-m "not real_api and not performance"`` for the main test job — this
    doesn't reference ``install_scripts`` at all, so before this fix the
    install-script E2E suite ran there too. The hook must still skip it.
    """
    result = install_scripts_sandbox.runpytest("-m", "not real_api and not performance")
    result.assert_outcomes(passed=1, skipped=1)


def test_install_scripts_runs_when_explicitly_selected(
    install_scripts_sandbox: pytest.Pytester,
) -> None:
    """``-m install_scripts`` opts in: the test must run (not be skipped by our hook)."""
    result = install_scripts_sandbox.runpytest("-m", "install_scripts")
    result.assert_outcomes(passed=1, deselected=1)


def test_install_scripts_deselected_by_make_test_expression(
    install_scripts_sandbox: pytest.Pytester,
) -> None:
    """``make test``'s ``-m "not install_scripts"`` must still deselect it.

    This expression explicitly references the marker, so pytest's own
    marker-expression evaluation (not our hook) does the deselecting.
    """
    result = install_scripts_sandbox.runpytest("-m", "not install_scripts")
    result.assert_outcomes(passed=1, deselected=1)


def test_regex_does_not_match_unrelated_marker_name(
    install_scripts_sandbox: pytest.Pytester,
) -> None:
    """A substring match on an unrelated marker must not be treated as opt-in.

    ``-m "not install_scripts_other"`` references a different (nonexistent)
    marker that merely contains "install_scripts" as a substring. It must
    not be confused with an explicit ``install_scripts`` reference, so the
    hook should still auto-skip the install_scripts test (proving the
    ``\\binstall_scripts\\b`` word-boundary regex, not a plain substring
    check, drives the opt-in detection).
    """
    result = install_scripts_sandbox.runpytest("-m", "not install_scripts_other")
    result.assert_outcomes(passed=1, skipped=1)


def test_suite_would_catch_a_reverted_hook(
    install_scripts_sandbox: pytest.Pytester,
) -> None:
    """Load-bearing check: a no-op hook must NOT skip the install_scripts test.

    Proves the ``skipped=1`` assertions above are meaningful (i.e. this
    suite would fail if the real hook were reverted/broken) rather than
    trivially passing regardless of hook behavior.
    """
    install_scripts_sandbox.makeconftest(
        """
def pytest_collection_modifyitems(config, items):
    pass
"""
    )
    result = install_scripts_sandbox.runpytest()
    result.assert_outcomes(passed=2)
