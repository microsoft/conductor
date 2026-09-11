"""Pytest configuration and shared fixtures for Conductor tests.

This module contains fixtures used across multiple test modules. It also
defines a collection hook (``pytest_collection_modifyitems``) that auto-skips
``@pytest.mark.real_api`` and ``@pytest.mark.install_scripts`` tests unless
explicitly selected via ``-m`` — see its docstring and issues #326 / #331 for
the full rationale.
"""

import re
import tempfile
from pathlib import Path

import pytest

# Enables the `pytester` fixture used by tests/test_config/test_real_api_marker.py
# and tests/test_config/test_install_scripts_marker.py to exercise this file's
# collection hook via an inner pytest run.
pytest_plugins = ["pytester"]

# Marker names that are opt-in by default: unless the caller's `-m`
# expression explicitly references one of these (by name, as a whole word),
# tests carrying it are skipped rather than executed. See the function
# docstring below for the full rationale and per-invocation behavior.
_OPT_IN_MARKER_NAMES = ("real_api", "install_scripts")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Make opt-in markers (``real_api``, ``install_scripts``) skip by default.

    Without this hook, nothing deselects tests carrying one of
    ``_OPT_IN_MARKER_NAMES`` unless the caller's ``-m`` expression already
    references that marker name (as CI/release workflows do). Both markers
    gate tests that can reach out and disrupt the *host* environment:
    ``real_api`` spawns real Copilot/Claude subprocesses (issue #326);
    ``install_scripts`` drives the install scripts' host-wide
    process-killing ``--auto-stop`` path (issue #331). Either can collide
    with and kill a live ``conductor run --web-bg`` session.

    This hook is load-bearing to different degrees per marker and per
    invocation: a plain ``pytest`` or ``pytest -m "not performance"`` never
    mentions either marker, so both are only caught by this hook. ``make
    test``'s own ``-m "not install_scripts"`` (see ``Makefile``) already
    deselects ``install_scripts`` independently via pytest's native
    marker-expression evaluation — but it never mentions ``real_api``, so
    that marker still relies on this hook there too.

    For each marker name, if the caller's ``-m`` expression already
    references it (e.g. ``-m real_api`` / ``-m install_scripts`` to opt in,
    or CI's ``-m "not real_api and not performance"``), pytest's own
    marker-expression evaluation already produces the correct
    selection/deselection, so this hook steps aside for that marker.
    """
    marker_expr = config.getoption("markexpr")
    for mark_name in _OPT_IN_MARKER_NAMES:
        # Matches the marker name as a whole word (not merely a substring)
        # inside the `-m` expression, e.g. "install_scripts", "not
        # install_scripts", "not real_api and not performance" all match for
        # their respective marker; "install_scripts_other" does not.
        if re.search(rf"\b{re.escape(mark_name)}\b", marker_expr):
            continue  # explicitly referenced; pytest's own evaluation handles it

        skip = pytest.mark.skip(reason=f"{mark_name} test: opt in with -m {mark_name}")
        for item in items:
            if mark_name in item.keywords:
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolate_event_log_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect ``tempfile.gettempdir()`` to this test's own ``tmp_path`` for
    *every* test, not just the ones that already know to isolate it.

    ``conductor.fleet.retention.event_log_root()`` (and every other caller
    of ``tempfile.gettempdir()`` -- ``engine/event_log.py``,
    ``cli/bg_runner.py``) resolve ``$TMPDIR/conductor/`` from this same
    function. Since Fleet Manager E5, ``run_workflow_async`` /
    ``resume_workflow_async`` call ``maybe_prune_event_logs()`` on startup
    with retention *enabled by default* (``keep_last = 200``) -- without
    this guard, any test that drives those functions (directly, or via a
    CLI command) prunes the *developer's actual* ``$TMPDIR/conductor/``,
    permanently deleting real ``conductor replay`` material. This was
    reproduced empirically: planting sentinel logs in the real
    ``/tmp/conductor`` and running a single wiring test alone deleted two of
    them.

    Returns pytest's own ``tmp_path`` verbatim (not a nested subdirectory
    of it) so that any other code in the same test which independently
    builds a path under ``tmp_path`` and compares it against
    ``tempfile.gettempdir()`` (e.g. ``mcp/manager.py``'s spill-dir
    symlink policy, which checks ``spill_dir.is_relative_to(temp_root)``)
    still sees a consistent root -- a nested subdirectory would make such
    paths spuriously "outside" the patched temp root and change behavior
    unrelated to this guard's purpose.

    An autouse fixture here means no future test needs to remember to
    isolate this itself. Tests that need a specific isolated root (e.g.
    ``tests/test_fleet/test_retention.py``'s own ``temp_root`` fixture) can
    still patch ``tempfile.gettempdir`` again themselves -- the later
    ``monkeypatch.setattr`` simply wins, and both this fixture and theirs
    ultimately point at a location under pytest's own ``tmp_path``, never
    the real system temp directory.
    """
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))


@pytest.fixture(autouse=True)
def _isolate_run_records_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect both run-record directories away from the developer's real home.

    The sibling guard above closes the ``$TMPDIR/conductor/`` leak; this one
    closes the ``~/.conductor/runs/`` half, which is the more destructive of
    the two. ``conductor.fleet.records.read_run_records()`` is a *pruning*
    reader: it deletes any record it judges corrupt, identity-mismatched, or
    owned by a dead PID. Anything that reaches it -- ``conductor stop``,
    ``fleet list``, the TUI poll, and ``retention.prune_event_logs()`` via
    ``_live_event_log_paths()`` -- therefore deletes the developer's *live*
    run records unless both directories are redirected.

    Both are needed because they resolve differently on purpose:
    ``records.run_records_dir()`` honors ``CONDUCTOR_HOME``, while
    ``cli.pid.pid_dir()`` deliberately does not (legacy ``.pid`` files must
    stay readable at their original, unredirected location). Redirecting only
    one leaves the other pointing at the real home, which is what
    ``tests/test_fleet/test_retention.py`` did.

    ``CONDUCTOR_HOME`` is set rather than patching ``run_records_dir``
    itself, so a test that sets the variable for its own purposes still
    wins -- patching the function would make that variable inert.
    """
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "conductor-home"))

    legacy_pid_dir = tmp_path / "legacy-pid-dir"

    def _isolated_pid_dir() -> Path:
        legacy_pid_dir.mkdir(parents=True, exist_ok=True)
        return legacy_pid_dir

    monkeypatch.setattr("conductor.cli.pid.pid_dir", _isolated_pid_dir)


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the path to the test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_workflow_yaml() -> str:
    """Return a minimal valid workflow YAML for testing."""
    return """\
workflow:
  name: test-workflow
  description: A test workflow
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello, world!"
    routes:
      - to: $end
"""


@pytest.fixture
def tmp_workflow_file(tmp_path: Path, sample_workflow_yaml: str) -> Path:
    """Create a temporary workflow YAML file."""
    workflow_file = tmp_path / "test-workflow.yaml"
    workflow_file.write_text(sample_workflow_yaml)
    return workflow_file


@pytest.fixture(autouse=True)
def _isolated_runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``conductor.rundir.runs_dir()`` at ``tmp_path`` for every test.

    ``conductor.rundir.runs_dir()`` backs both the PID registry
    (``cli/pid.py``) and the dashboard token file (``web/auth.py``, issue
    #397); several call paths (``conductor gate respond`` / ``guide`` /
    the graceful-kill rung of ``conductor stop``, and ``WebDashboard.start``/
    ``stop``) read or write it when no explicit ``--token``/env var is
    supplied. Making this autouse and global — rather than the four
    hand-copied per-file versions it replaces — closes the gap where a test
    module that never opted in (``test_markup_injection.py``) read the
    developer's real ``~/.conductor/runs`` and, on a machine with a live
    dashboard, its live token.

    Also asserts, on every call, that the resolved directory is never the
    real home — a meta-guard in case a future test bypasses the monkeypatch
    (e.g. by importing ``runs_dir`` before this fixture runs).
    """
    runs_dir = tmp_path / "runs_isolated_for_tests"
    runs_dir.mkdir()
    real_home_runs_dir = Path.home() / ".conductor" / "runs"

    def _isolated() -> Path:
        assert runs_dir != real_home_runs_dir
        return runs_dir

    monkeypatch.setattr("conductor.rundir.runs_dir", _isolated)


# Test modules that genuinely exercise `ClaudeAgentSdkProvider._check_auth_readiness`
# itself (calling the real method against a mocked subprocess, or asserting on its
# exact CLI-probe/timeout/interrupt behavior) and must therefore receive the *real*
# implementation rather than the hermetic stub below. Matched by module basename
# (without `.py`) so it is independent of which directory collects the file.
_CLAUDE_AUTH_READINESS_HERMETIC_EXEMPT_MODULES = frozenset(
    {
        "test_claude_agent_sdk_auth",
        "test_claude_agent_sdk",
    }
)


@pytest.fixture(autouse=True)
def _stub_claude_auth_readiness(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub ``ClaudeAgentSdkProvider._check_auth_readiness`` to a deterministic,
    already-ready result for every test, repository-wide (TICKET-20260816-0002).

    ``execute()`` now runs a real, hard-timeout-bounded ``claude auth status
    --json`` preflight before every agent turn (see ``_check_auth_readiness``
    in ``providers/claude_agent_sdk.py``). Without this fixture, *any* test
    that drives ``ClaudeAgentSdkProvider.execute()`` -- directly or via
    ``WorkflowEngine`` -- spawns that real subprocess: on a developer machine
    with the ``claude`` CLI installed this silently depends on local login
    state; on upstream CI (no CLI, no ``ANTHROPIC_API_KEY``) every such test
    fails or hangs on the readiness check instead of exercising its own
    behavior. Per-file ad hoc patches (as several test modules already carry)
    do not scale to the next test someone writes, and two modules in
    particular -- ``tests/test_integration/test_session_key_continuity.py``
    and ``tests/test_providers/test_claude_agent_sdk_session_key.py`` -- carry
    no such patch at all.

    This is a pure hermeticity guard, not a behavior assertion: it always
    reports an *inferred* subscription readiness, regardless of the
    provider's configured ``auth_mode``, matching this fixture's only job
    (let unrelated tests run without touching a real CLI or real
    credentials) rather than modeling any particular auth outcome.

    A test in a module listed in
    :data:`_CLAUDE_AUTH_READINESS_HERMETIC_EXEMPT_MODULES` -- one that
    genuinely exercises the preflight itself (its CLI-probe path set,
    timeout, or exact ``ClaudeAuthStatus`` it derives) against a mocked
    subprocess -- receives the real, unstubbed method instead. A test
    elsewhere that needs to assert on a *specific* readiness outcome (e.g. a
    not-ready/error status) still overrides this default in the usual way,
    via ``patch.object(provider, "_check_auth_readiness", ...)`` scoped to
    that test -- an instance-level patch always wins over this fixture's
    class-level default.
    """
    module = request.node.module
    module_name = module.__name__.rsplit(".", 1)[-1] if module is not None else ""
    if module_name in _CLAUDE_AUTH_READINESS_HERMETIC_EXEMPT_MODULES:
        return

    from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider, ClaudeAuthStatus

    async def _always_ready(self: ClaudeAgentSdkProvider) -> ClaudeAuthStatus:
        return ClaudeAuthStatus(
            requested_mode=self._auth_mode,
            inferred_mode="subscription",
            ready=True,
        )

    monkeypatch.setattr(ClaudeAgentSdkProvider, "_check_auth_readiness", _always_ready)
