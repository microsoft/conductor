"""Tests for launch-directory resolution (issue #544): Copilot-host ancestor
detection, explicit-override precedence, normalization, and validation.

The process-inspection seam (``_parent_process_info``) is mocked throughout
so these tests never depend on the developer's actual process tree or an
actual Copilot session -- only :class:`TestRealAncestryIsHarmless` touches
the real ``psutil``-backed function, and only to prove it does not raise
when this test process (not launched by Copilot) is inspected for real.
"""

from __future__ import annotations

import os
from pathlib import Path

import psutil
import pytest

from conductor.mcp.serve import launch_dir as launch_dir_module
from conductor.mcp.serve.launch_dir import (
    LaunchDirectoryError,
    _AncestorScan,
    _ProcessInfo,
    _scan_for_copilot_ancestor,
    detect_copilot_ancestor_cwd,
    normalize_launch_dir,
    resolve_launch_dir,
    validate_launch_dir,
)
from conductor.mcp.serve.options import ServeOptions

# ---------------------------------------------------------------------------
# Mocking the process-inspection seam
# ---------------------------------------------------------------------------


def _mock_ancestry(
    monkeypatch: pytest.MonkeyPatch, chain: dict[int, _ProcessInfo | BaseException]
) -> None:
    """Replace ``_parent_process_info`` with a lookup against *chain*.

    ``chain[pid]`` is the info about *pid*'s immediate parent -- an
    exception instance is raised instead of returned, so a test can
    simulate an ``AccessDenied`` (or any other) failure at a specific
    point in the walk. A ``pid`` absent from *chain* means "no parent" --
    the ordinary end of an ancestry walk.
    """

    def _fake(pid: int) -> _ProcessInfo | None:
        if pid not in chain:
            return None
        value = chain[pid]
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(launch_dir_module, "_parent_process_info", _fake)


_SELF_PID = 1
"""An arbitrary "this process" pid for building synthetic ancestries --
never a real pid, since ``_parent_process_info`` is always mocked in these
tests."""


# ---------------------------------------------------------------------------
# Ancestor detection
# ---------------------------------------------------------------------------


class TestScanForCopilotAncestor:
    def test_no_ancestor_at_all_is_ordinary_not_limited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The common case: running the server directly from a terminal.
        No Copilot ancestor exists, and that is not a limitation."""
        _mock_ancestry(monkeypatch, {})

        scan = _scan_for_copilot_ancestor(_SELF_PID)

        assert scan == _AncestorScan(cwd=None, limited=False)

    def test_direct_copilot_parent_is_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        host_cwd = Path("/host/project")
        _mock_ancestry(
            monkeypatch,
            {_SELF_PID: _ProcessInfo(pid=2, name="copilot", exe_basename="copilot", cwd=host_cwd)},
        )

        scan = _scan_for_copilot_ancestor(_SELF_PID)

        assert scan == _AncestorScan(cwd=host_cwd, limited=False)

    def test_windows_executable_name_is_recognized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        host_cwd = Path("C:/Users/dev/project")
        _mock_ancestry(
            monkeypatch,
            {
                _SELF_PID: _ProcessInfo(
                    pid=2, name="copilot.exe", exe_basename="copilot.exe", cwd=host_cwd
                )
            },
        )

        scan = _scan_for_copilot_ancestor(_SELF_PID)

        assert scan.cwd == host_cwd

    def test_process_name_differing_from_executable_basename_is_still_recognized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real Copilot ancestor reported a `psutil` process ``name()``
        of ``MainThread`` while its executable basename remained
        ``copilot`` -- detection must match on the executable, not the
        process-manager-reported name, or it misses the host entirely
        (issue #544 P1)."""
        host_cwd = Path("/host/project")
        _mock_ancestry(
            monkeypatch,
            {
                _SELF_PID: _ProcessInfo(
                    pid=2, name="MainThread", exe_basename="copilot", cwd=host_cwd
                )
            },
        )

        scan = _scan_for_copilot_ancestor(_SELF_PID)

        assert scan == _AncestorScan(cwd=host_cwd, limited=False)

    def test_walks_through_an_intervening_launch_wrapper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The MCP server's direct parent is a `node`/npm wrapper the
        Copilot CLI spawned it through; the Copilot host is one hop
        further up."""
        host_cwd = Path("/host/project")
        _mock_ancestry(
            monkeypatch,
            {
                _SELF_PID: _ProcessInfo(
                    pid=2, name="node", exe_basename="node", cwd=Path("/npm/wrapper/dir")
                ),
                2: _ProcessInfo(pid=3, name="copilot", exe_basename="copilot", cwd=host_cwd),
            },
        )

        scan = _scan_for_copilot_ancestor(_SELF_PID)

        assert scan == _AncestorScan(cwd=host_cwd, limited=False)

    def test_nearest_copilot_ancestor_wins_over_a_further_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A nested Copilot session (a Copilot process launched from
        within another) resolves to the *nearer* one."""
        nearer_cwd = Path("/nearer/project")
        further_cwd = Path("/further/project")
        _mock_ancestry(
            monkeypatch,
            {
                _SELF_PID: _ProcessInfo(
                    pid=2, name="copilot", exe_basename="copilot", cwd=nearer_cwd
                ),
                2: _ProcessInfo(pid=3, name="copilot", exe_basename="copilot", cwd=further_cwd),
            },
        )

        scan = _scan_for_copilot_ancestor(_SELF_PID)

        assert scan.cwd == nearer_cwd

    def test_unrelated_process_name_is_not_mistaken_for_the_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A process merely containing "copilot" in its name (but not
        named exactly ``copilot``/``copilot.exe``) must never match --
        detection matches the executable name precisely, not a substring
        search over arbitrary process names or command lines."""
        _mock_ancestry(
            monkeypatch,
            {
                _SELF_PID: _ProcessInfo(
                    pid=2,
                    name="my-copilot-wrapper",
                    exe_basename="my-copilot-wrapper",
                    cwd=Path("/wrapper"),
                ),
                2: _ProcessInfo(
                    pid=3, name="copilot-clone", exe_basename="copilot-clone", cwd=Path("/clone")
                ),
            },
        )

        scan = _scan_for_copilot_ancestor(_SELF_PID)

        assert scan == _AncestorScan(cwd=None, limited=False)

    def test_cyclic_ancestry_terminates_as_a_limitation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed or cyclic parent chain must not loop forever."""
        _mock_ancestry(
            monkeypatch,
            {
                _SELF_PID: _ProcessInfo(
                    pid=2, name="wrapper", exe_basename="wrapper", cwd=Path("/a")
                ),
                2: _ProcessInfo(
                    pid=_SELF_PID, name="wrapper", exe_basename="wrapper", cwd=Path("/b")
                ),  # cycle
            },
        )

        scan = _scan_for_copilot_ancestor(_SELF_PID)

        assert scan.cwd is None

    def test_bounded_walk_without_reaching_the_top_is_a_limitation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A chain deeper than the hop bound, with no Copilot ancestor
        found and no clean top reached, is a limitation (a real Copilot
        ancestor could still be further up) -- not an ordinary miss."""

        def _ever_deeper(pid: int) -> _ProcessInfo | None:
            return _ProcessInfo(
                pid=pid + 1, name="wrapper", exe_basename="wrapper", cwd=Path(f"/level{pid}")
            )

        monkeypatch.setattr(launch_dir_module, "_parent_process_info", _ever_deeper)

        scan = _scan_for_copilot_ancestor(_SELF_PID)

        assert scan == _AncestorScan(cwd=None, limited=True)

    def test_access_denied_reading_a_parent_is_a_limitation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_ancestry(monkeypatch, {_SELF_PID: psutil.AccessDenied()})

        scan = _scan_for_copilot_ancestor(_SELF_PID)

        assert scan == _AncestorScan(cwd=None, limited=True)

    def test_vanished_process_mid_walk_is_ordinary_not_limited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_parent_process_info` itself already absorbs `NoSuchProcess`/
        `ZombieProcess` into a `None` return (an ordinary end of the
        chain), so the scan must not treat that as a limitation."""
        _mock_ancestry(monkeypatch, {})  # mirrors `_parent_process_info` returning None

        scan = _scan_for_copilot_ancestor(_SELF_PID)

        assert scan.limited is False

    def test_copilot_host_with_unreadable_cwd_raises_rather_than_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A positively identified Copilot host whose cwd cannot be read
        must fail loudly -- launching in this process's own (likely
        plugin-install) directory would be worse than an actionable
        error."""
        _mock_ancestry(
            monkeypatch,
            {_SELF_PID: _ProcessInfo(pid=2, name="copilot", exe_basename="copilot", cwd=None)},
        )

        with pytest.raises(LaunchDirectoryError, match="working directory"):
            _scan_for_copilot_ancestor(_SELF_PID)


class TestDetectCopilotAncestorCwd:
    def test_returns_none_and_does_not_warn_when_no_host_is_found(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _mock_ancestry(monkeypatch, {})

        with caplog.at_level("WARNING"):
            result = detect_copilot_ancestor_cwd(_SELF_PID)

        assert result is None
        assert caplog.records == []

    def test_returns_the_detected_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        host_cwd = Path("/host/project")
        _mock_ancestry(
            monkeypatch,
            {_SELF_PID: _ProcessInfo(pid=2, name="copilot", exe_basename="copilot", cwd=host_cwd)},
        )

        assert detect_copilot_ancestor_cwd(_SELF_PID) == host_cwd

    def test_warns_when_the_walk_was_limited(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _mock_ancestry(monkeypatch, {_SELF_PID: psutil.AccessDenied()})

        with caplog.at_level("WARNING"):
            result = detect_copilot_ancestor_cwd(_SELF_PID)

        assert result is None
        assert any("ancestry" in record.message for record in caplog.records)

    def test_defaults_to_this_process_pid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitting `pid` walks ancestors of `os.getpid()`."""
        seen_pids: list[int] = []

        def _fake(pid: int) -> _ProcessInfo | None:
            seen_pids.append(pid)
            return None

        monkeypatch.setattr(launch_dir_module, "_parent_process_info", _fake)

        detect_copilot_ancestor_cwd()

        assert seen_pids == [os.getpid()]


# ---------------------------------------------------------------------------
# Explicit override precedence / detection bypass
# ---------------------------------------------------------------------------


class TestResolveLaunchDirPrecedence:
    def test_explicit_override_bypasses_detection_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(pid: int) -> _ProcessInfo | None:
            raise AssertionError("ancestor detection must not run when explicit is given")

        monkeypatch.setattr(launch_dir_module, "_parent_process_info", _boom)

        result = resolve_launch_dir(Path("/explicit/path"))

        assert result == Path("/explicit/path")

    def test_detected_cwd_is_used_when_no_explicit_value_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host_cwd = Path("/host/project")
        _mock_ancestry(
            monkeypatch,
            {_SELF_PID: _ProcessInfo(pid=2, name="copilot", exe_basename="copilot", cwd=host_cwd)},
        )
        monkeypatch.setattr(launch_dir_module, "detect_copilot_ancestor_cwd", lambda: host_cwd)

        result = resolve_launch_dir(None)

        assert result == host_cwd

    def test_falls_back_to_server_cwd_when_no_host_detected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(launch_dir_module, "detect_copilot_ancestor_cwd", lambda: None)

        result = resolve_launch_dir(None)

        assert result == Path.cwd()


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalizeLaunchDir:
    def test_expands_home(self) -> None:
        result = normalize_launch_dir(Path("~"))
        assert result == Path(os.path.expanduser("~"))
        assert result.is_absolute()

    def test_makes_a_relative_path_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = normalize_launch_dir(Path("subdir"))
        assert result == tmp_path / "subdir"
        assert result.is_absolute()

    def test_leaves_a_symlink_unresolved(self, tmp_path: Path) -> None:
        """normpath, not resolve: a symlinked directory stays the alias it
        was given rather than being collapsed to its real path."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "alias"
        link.symlink_to(real_dir)

        result = normalize_launch_dir(link)

        assert result == link
        assert result != real_dir.resolve()

    def test_already_absolute_path_is_unchanged(self, tmp_path: Path) -> None:
        assert normalize_launch_dir(tmp_path) == tmp_path


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidateLaunchDir:
    def test_valid_directory_passes(self, tmp_path: Path) -> None:
        validate_launch_dir(tmp_path)  # must not raise

    def test_missing_path_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(LaunchDirectoryError, match="does not exist"):
            validate_launch_dir(tmp_path / "does-not-exist")

    def test_a_file_is_rejected(self, tmp_path: Path) -> None:
        file_path = tmp_path / "not-a-dir.txt"
        file_path.write_text("hello", encoding="utf-8")

        with pytest.raises(LaunchDirectoryError, match="not a directory"):
            validate_launch_dir(file_path)

    def test_every_error_names_the_launch_dir_remedy(self, tmp_path: Path) -> None:
        with pytest.raises(LaunchDirectoryError) as excinfo:
            validate_launch_dir(tmp_path / "gone")
        assert "--launch-dir" in str(excinfo.value)

    def test_an_os_error_while_checking_is_reported_as_inaccessible(self) -> None:
        class _RaisingPath:
            def is_dir(self) -> bool:
                raise OSError("Permission denied")

            def __str__(self) -> str:
                return "/fake/inaccessible"

        with pytest.raises(LaunchDirectoryError, match="Could not access"):
            validate_launch_dir(_RaisingPath())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ServeOptions integration
# ---------------------------------------------------------------------------


class TestServeOptionsIntegration:
    def test_default_launch_dir_is_normalized_and_validated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(launch_dir_module, "detect_copilot_ancestor_cwd", lambda: None)
        monkeypatch.chdir(tmp_path)

        options = ServeOptions()

        assert options.launch_dir == tmp_path

    def test_explicit_launch_dir_is_normalized_the_same_way(self, tmp_path: Path) -> None:
        options = ServeOptions(launch_dir=Path("~"))
        assert options.launch_dir == Path(os.path.expanduser("~"))

    def test_relative_explicit_launch_dir_is_absolutized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()

        options = ServeOptions(launch_dir=Path("sub"))

        assert options.launch_dir == sub

    def test_invalid_explicit_launch_dir_raises_at_construction(self, tmp_path: Path) -> None:
        with pytest.raises(LaunchDirectoryError):
            ServeOptions(launch_dir=tmp_path / "nope")

    def test_options_are_frozen(self, tmp_path: Path) -> None:
        options = ServeOptions(launch_dir=tmp_path)
        with pytest.raises(Exception):  # noqa: B017,PT011 - dataclasses.FrozenInstanceError
            options.launch_dir = tmp_path / "other"  # type: ignore[misc]

    def test_a_host_directory_change_after_construction_does_not_affect_the_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The launch directory is captured once at construction; later
        changes to what a (mocked) Copilot host's cwd would resolve to
        must never retroactively change an already-built `ServeOptions`."""
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()

        current_host_cwd = {"value": first_dir}
        monkeypatch.setattr(
            launch_dir_module,
            "detect_copilot_ancestor_cwd",
            lambda: current_host_cwd["value"],
        )

        first_options = ServeOptions()
        assert first_options.launch_dir == first_dir

        # The "host" cwd changes -- e.g. the operator's IDE switches
        # projects -- after the first ServeOptions snapshot was taken.
        current_host_cwd["value"] = second_dir

        # The already-constructed options must be unaffected.
        assert first_options.launch_dir == first_dir

        # A *new* ServeOptions (as a server restart would produce) takes a
        # fresh snapshot and does see the change.
        second_options = ServeOptions()
        assert second_options.launch_dir == second_dir


# ---------------------------------------------------------------------------
# Real (unmocked) process inspection -- a light smoke test
# ---------------------------------------------------------------------------


class TestRealAncestryIsHarmless:
    def test_detecting_against_the_real_process_tree_does_not_raise(self) -> None:
        """This test process was not launched by Copilot, so detection
        should cleanly find nothing -- exercised against the real
        `psutil`-backed seam (no mocking) to prove the integration itself
        does not raise on an ordinary process tree."""
        result = detect_copilot_ancestor_cwd()
        assert result is None or isinstance(result, Path)
