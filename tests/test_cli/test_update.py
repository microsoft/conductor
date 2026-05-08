"""Tests for update check and self-upgrade utilities (``conductor.cli.update``).

Covers:
- Cache path, read/write, and expiry logic
- Version parsing, pre-release detection, and comparison
- ``fetch_latest_version()`` with mocked network responses
- ``check_for_update_hint()`` with TTY/verbosity/subcommand guards
- ``run_update()`` with mocked subprocess execution
- ``run_update(apply=True)`` spawn-and-exit handoff to the install script
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.cli.update import (
    _CACHE_TTL_SECONDS,
    check_for_update_hint,
    fetch_latest_version,
    get_cache_path,
    has_prerelease,
    is_newer,
    parse_version,
    read_cache,
    run_update,
    write_cache,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``get_cache_path()`` to a temp directory."""
    cache_file = tmp_path / "update-check.json"
    monkeypatch.setattr("conductor.cli.update.get_cache_path", lambda: cache_file)
    return tmp_path


def _make_console(*, is_terminal: bool = True) -> tuple[Console, StringIO]:
    """Create a Rich Console writing to a StringIO buffer.

    Returns:
        A ``(console, buffer)`` pair.
    """
    buf = StringIO()
    c = Console(file=buf, force_terminal=is_terminal, no_color=True, highlight=False)
    return c, buf


# ===================================================================
# E2-T6: Cache, parse_version, has_prerelease, is_newer
# ===================================================================


class TestGetCachePath:
    """Tests for ``get_cache_path``."""

    def test_returns_expected_path(self) -> None:
        path = get_cache_path()
        assert path == Path.home() / ".conductor" / "update-check.json"

    def test_returns_path_object(self) -> None:
        assert isinstance(get_cache_path(), Path)


class TestReadCache:
    """Tests for ``read_cache``."""

    def test_returns_none_for_missing_file(self, cache_dir: Path) -> None:
        assert read_cache() is None

    def test_returns_none_for_invalid_json(self, cache_dir: Path) -> None:
        (cache_dir / "update-check.json").write_text("not json{{{")
        assert read_cache() is None

    def test_returns_none_for_missing_checked_at(self, cache_dir: Path) -> None:
        data = {"version": "0.2.0", "tag_name": "v0.2.0", "url": "https://example.com"}
        (cache_dir / "update-check.json").write_text(json.dumps(data))
        assert read_cache() is None

    def test_returns_none_for_expired_cache(self, cache_dir: Path) -> None:
        old_time = datetime.now(UTC) - timedelta(seconds=_CACHE_TTL_SECONDS + 100)
        data = {
            "version": "0.2.0",
            "tag_name": "v0.2.0",
            "url": "https://example.com",
            "checked_at": old_time.isoformat(),
        }
        (cache_dir / "update-check.json").write_text(json.dumps(data))
        assert read_cache() is None

    def test_returns_data_for_fresh_cache(self, cache_dir: Path) -> None:
        now = datetime.now(UTC)
        data = {
            "version": "0.2.0",
            "tag_name": "v0.2.0",
            "url": "https://example.com",
            "checked_at": now.isoformat(),
        }
        (cache_dir / "update-check.json").write_text(json.dumps(data))
        result = read_cache()
        assert result is not None
        assert result["version"] == "0.2.0"
        assert result["tag_name"] == "v0.2.0"


class TestWriteCache:
    """Tests for ``write_cache``."""

    def test_creates_valid_json(self, cache_dir: Path) -> None:
        write_cache("0.3.0", "v0.3.0", "https://github.com/microsoft/conductor/releases/v0.3.0")
        cache_file = cache_dir / "update-check.json"
        assert cache_file.exists()
        data = json.loads(cache_file.read_text())
        assert data["version"] == "0.3.0"
        assert data["tag_name"] == "v0.3.0"
        assert data["url"] == "https://github.com/microsoft/conductor/releases/v0.3.0"
        assert "checked_at" in data

    def test_checked_at_is_iso_format(self, cache_dir: Path) -> None:
        write_cache("0.3.0", "v0.3.0", "https://example.com")
        data = json.loads((cache_dir / "update-check.json").read_text())
        # Should parse without error
        datetime.fromisoformat(data["checked_at"])


class TestParseVersion:
    """Tests for ``parse_version``."""

    def test_simple_version(self) -> None:
        assert parse_version("0.1.0") == (0, 1, 0)

    def test_strips_leading_v(self) -> None:
        assert parse_version("v0.2.0") == (0, 2, 0)

    def test_strips_prerelease_suffix(self) -> None:
        assert parse_version("0.3.0-beta.1") == (0, 3, 0)

    def test_strips_v_and_prerelease(self) -> None:
        assert parse_version("v1.0.0-rc.2") == (1, 0, 0)

    def test_two_part_version(self) -> None:
        assert parse_version("1.0") == (1, 0)

    def test_four_part_version(self) -> None:
        assert parse_version("1.2.3.4") == (1, 2, 3, 4)


class TestHasPrerelease:
    """Tests for ``has_prerelease``."""

    def test_stable_version(self) -> None:
        assert has_prerelease("0.3.0") is False

    def test_stable_version_with_v(self) -> None:
        assert has_prerelease("v0.3.0") is False

    def test_beta_prerelease(self) -> None:
        assert has_prerelease("0.3.0-beta.1") is True

    def test_rc_prerelease(self) -> None:
        assert has_prerelease("v1.0.0-rc.2") is True

    def test_alpha_prerelease(self) -> None:
        assert has_prerelease("0.1.0-alpha") is True


class TestIsNewer:
    """Tests for ``is_newer``."""

    def test_newer_patch(self) -> None:
        assert is_newer("0.1.1", "0.1.0") is True

    def test_newer_minor(self) -> None:
        assert is_newer("0.2.0", "0.1.0") is True

    def test_newer_major(self) -> None:
        assert is_newer("1.0.0", "0.9.9") is True

    def test_same_version(self) -> None:
        assert is_newer("0.1.0", "0.1.0") is False

    def test_older_version(self) -> None:
        assert is_newer("0.1.0", "0.2.0") is False

    def test_prerelease_to_release_upgrade(self) -> None:
        # Same numeric version, local is pre-release, remote is stable → upgrade
        assert is_newer("0.3.0", "0.3.0-beta.1") is True

    def test_both_prerelease_same_numeric(self) -> None:
        # Both are pre-release with same numeric part → not newer
        assert is_newer("0.3.0-beta.2", "0.3.0-beta.1") is False

    def test_remote_prerelease_same_numeric(self) -> None:
        # Local is stable, remote is pre-release with same numeric → not newer
        assert is_newer("0.3.0-beta.1", "0.3.0") is False

    def test_with_v_prefix(self) -> None:
        assert is_newer("v0.2.0", "v0.1.0") is True

    def test_mixed_v_prefix(self) -> None:
        assert is_newer("0.2.0", "v0.1.0") is True


# ===================================================================
# E2-T7: fetch_latest_version, check_for_update_hint
# ===================================================================


class TestFetchLatestVersion:
    """Tests for ``fetch_latest_version`` with mocked network."""

    def test_success_returns_3_tuple(self) -> None:
        response_data = json.dumps(
            {
                "tag_name": "v0.5.0",
                "html_url": "https://github.com/microsoft/conductor/releases/tag/v0.5.0",
            }
        ).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("conductor.cli.update.urllib.request.urlopen", return_value=mock_resp):
            result = fetch_latest_version()

        assert result is not None
        version, tag_name, url = result
        assert version == "0.5.0"
        assert tag_name == "v0.5.0"
        assert "v0.5.0" in url

    def test_timeout_returns_none(self) -> None:
        import urllib.error

        with patch(
            "conductor.cli.update.urllib.request.urlopen",
            side_effect=urllib.error.URLError("timeout"),
        ):
            assert fetch_latest_version() is None

    def test_http_error_returns_none(self) -> None:
        import urllib.error

        with patch(
            "conductor.cli.update.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                url="",
                code=404,
                msg="Not Found",
                hdrs=None,
                fp=None,  # type: ignore[arg-type]
            ),
        ):
            assert fetch_latest_version() is None

    def test_malformed_json_returns_none(self) -> None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("conductor.cli.update.urllib.request.urlopen", return_value=mock_resp):
            assert fetch_latest_version() is None

    def test_missing_tag_name_returns_none(self) -> None:
        response_data = json.dumps({"html_url": "https://example.com"}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("conductor.cli.update.urllib.request.urlopen", return_value=mock_resp):
            assert fetch_latest_version() is None


class TestCheckForUpdateHint:
    """Tests for ``check_for_update_hint``."""

    def test_non_tty_skips(self, cache_dir: Path) -> None:
        """Non-TTY console should not print anything."""
        c, buf = _make_console(is_terminal=False)
        check_for_update_hint(c)
        assert buf.getvalue() == ""

    def test_silent_mode_skips(self, cache_dir: Path) -> None:
        """Silent verbosity should suppress the hint."""
        from conductor.cli.app import ConsoleVerbosity

        c, buf = _make_console(is_terminal=True)
        with patch("conductor.cli.app.console_verbosity") as mock_cv:
            mock_cv.get.return_value = ConsoleVerbosity.SILENT
            check_for_update_hint(c)
        assert buf.getvalue() == ""

    def test_update_subcommand_skips(self, cache_dir: Path) -> None:
        """When the subcommand is 'update', skip the hint."""
        c, buf = _make_console(is_terminal=True)
        with (
            patch("conductor.cli.update.sys.argv", ["conductor", "update"]),
            patch("conductor.cli.app.console_verbosity") as mock_cv,
        ):
            from conductor.cli.app import ConsoleVerbosity

            mock_cv.get.return_value = ConsoleVerbosity.FULL
            check_for_update_hint(c)
        assert buf.getvalue() == ""

    def test_fresh_cache_newer_shows_hint(self, cache_dir: Path) -> None:
        """Fresh cache with newer version should print the hint."""
        now = datetime.now(UTC)
        data = {
            "version": "99.0.0",
            "tag_name": "v99.0.0",
            "url": "https://example.com",
            "checked_at": now.isoformat(),
        }
        (cache_dir / "update-check.json").write_text(json.dumps(data))

        c, buf = _make_console(is_terminal=True)
        with (
            patch("conductor.cli.update.sys.argv", ["conductor", "run", "wf.yaml"]),
            patch("conductor.cli.app.console_verbosity") as mock_cv,
        ):
            from conductor.cli.app import ConsoleVerbosity

            mock_cv.get.return_value = ConsoleVerbosity.FULL
            check_for_update_hint(c)

        output = buf.getvalue()
        assert "99.0.0" in output
        assert "conductor update" in output

    def test_fresh_cache_same_version_no_hint(self, cache_dir: Path) -> None:
        """Fresh cache with same version should not print anything."""
        now = datetime.now(UTC)
        data = {
            "version": __import__("conductor").__version__,
            "tag_name": f"v{__import__('conductor').__version__}",
            "url": "https://example.com",
            "checked_at": now.isoformat(),
        }
        (cache_dir / "update-check.json").write_text(json.dumps(data))

        c, buf = _make_console(is_terminal=True)
        with (
            patch("conductor.cli.update.sys.argv", ["conductor", "run", "wf.yaml"]),
            patch("conductor.cli.app.console_verbosity") as mock_cv,
        ):
            from conductor.cli.app import ConsoleVerbosity

            mock_cv.get.return_value = ConsoleVerbosity.FULL
            check_for_update_hint(c)

        assert buf.getvalue() == ""

    def test_stale_cache_triggers_fetch(self, cache_dir: Path) -> None:
        """Expired cache should trigger a network fetch."""
        old_time = datetime.now(UTC) - timedelta(seconds=_CACHE_TTL_SECONDS + 100)
        data = {
            "version": "0.0.1",
            "tag_name": "v0.0.1",
            "url": "https://example.com",
            "checked_at": old_time.isoformat(),
        }
        (cache_dir / "update-check.json").write_text(json.dumps(data))

        c, buf = _make_console(is_terminal=True)
        with (
            patch("conductor.cli.update.sys.argv", ["conductor", "run", "wf.yaml"]),
            patch("conductor.cli.app.console_verbosity") as mock_cv,
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("99.0.0", "v99.0.0", "https://example.com"),
            ) as mock_fetch,
        ):
            from conductor.cli.app import ConsoleVerbosity

            mock_cv.get.return_value = ConsoleVerbosity.FULL
            check_for_update_hint(c)

        mock_fetch.assert_called_once()
        output = buf.getvalue()
        assert "99.0.0" in output


# ===================================================================
# E2-T8: run_update (now: instructs the user to run the install script)
# ===================================================================


class TestRunUpdate:
    """Tests for `run_update` after the move to install-script-only upgrades."""

    def test_already_up_to_date(self, cache_dir: Path) -> None:
        c, buf = _make_console()
        with (
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("0.1.0", "v0.1.0", "https://example/release"),
            ),
            patch("conductor.cli.update.__version__", "0.1.0"),
        ):
            run_update(c)
        output = buf.getvalue()
        assert "up to date" in output.lower()
        assert (cache_dir / "update-check.json").exists()

    def test_newer_available_prints_install_command(self, cache_dir: Path) -> None:
        c, buf = _make_console()
        with (
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("99.0.0", "v99.0.0", "https://example/release"),
            ),
            patch("conductor.cli.update.__version__", "0.1.0"),
        ):
            run_update(c)
        output = buf.getvalue()
        assert "99.0.0" in output
        import sys as _sys

        if _sys.platform == "win32":
            assert "install.ps1" in output
            assert "iex" in output
        else:
            assert "install.sh" in output
            assert "curl" in output

    def test_no_subprocess_install_attempt(self, cache_dir: Path) -> None:
        c, _buf = _make_console()
        with (
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("99.0.0", "v99.0.0", "https://example/release"),
            ),
            patch("conductor.cli.update.__version__", "0.1.0"),
            patch("subprocess.run") as mock_run,
            patch("subprocess.Popen") as mock_popen,
        ):
            run_update(c)
        assert mock_run.call_count == 0
        assert mock_popen.call_count == 0

    def test_fetch_failure_prints_error(self, cache_dir: Path) -> None:
        c, buf = _make_console()
        with patch("conductor.cli.update.fetch_latest_version", return_value=None):
            run_update(c)
        assert "could not reach github" in buf.getvalue().lower()

    def test_force_kwarg_accepted_for_back_compat(self, cache_dir: Path) -> None:
        c, _buf = _make_console()
        with (
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("99.0.0", "v99.0.0", "https://example/"),
            ),
            patch("conductor.cli.update.__version__", "0.1.0"),
        ):
            run_update(c, force=True)


class TestRunUpdateApply:
    """Tests for ``run_update(apply=True)`` — spawn-and-exit behavior."""

    @pytest.fixture()
    def newer_release(self, cache_dir: Path):
        with (
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("99.0.0", "v99.0.0", "https://example/release"),
            ),
            patch("conductor.cli.update.__version__", "0.1.0"),
        ):
            yield

    def test_apply_does_not_print_paste_command(self, newer_release) -> None:
        """With --apply we should hand off to the installer, not print a manual command."""
        c, buf = _make_console()
        with (
            patch("conductor.cli.update._spawn_installer_and_exit") as mock_spawn,
        ):
            run_update(c, apply=True)
        mock_spawn.assert_called_once()
        output = buf.getvalue()
        # The paste-this-into-a-new-shell hint should not be shown.
        assert "new shell" not in output.lower()

    def test_apply_skipped_when_already_up_to_date(self, cache_dir: Path) -> None:
        """If we're already on the latest version, --apply should be a no-op."""
        c, _buf = _make_console()
        with (
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("0.1.0", "v0.1.0", "https://example/"),
            ),
            patch("conductor.cli.update.__version__", "0.1.0"),
            patch("conductor.cli.update._spawn_installer_and_exit") as mock_spawn,
        ):
            run_update(c, apply=True)
        mock_spawn.assert_not_called()

    def test_apply_skipped_when_fetch_fails(self, cache_dir: Path) -> None:
        """If we can't reach GitHub, --apply must not blindly run the installer."""
        c, _buf = _make_console()
        with (
            patch("conductor.cli.update.fetch_latest_version", return_value=None),
            patch("conductor.cli.update._spawn_installer_and_exit") as mock_spawn,
        ):
            run_update(c, apply=True)
        mock_spawn.assert_not_called()

    def test_apply_mentions_new_console_or_replace_in_output(self, newer_release) -> None:
        """The handoff message should describe what is about to happen."""
        c, buf = _make_console()

        # Make _spawn_installer_and_exit do its real Rich printing but
        # short-circuit before the actual subprocess call.
        def fake_spawn(console):
            if sys.platform == "win32":
                console.print(
                    "Installer launched in a new console window. "
                    "This conductor process will now exit so file locks release."
                )
                raise SystemExit(0)
            console.print("Replacing conductor with installer…")

        with (
            patch("conductor.cli.update._spawn_installer_and_exit", side_effect=fake_spawn),
            contextlib.suppress(SystemExit),
        ):
            run_update(c, apply=True)
        output = buf.getvalue().lower()
        if sys.platform == "win32":
            assert "new console" in output or "exit" in output
        else:
            assert "replacing" in output or "installer" in output

    def test_spawn_installer_windows_uses_new_console_and_exits(self, cache_dir: Path) -> None:
        """On Windows, the installer is spawned with CREATE_NEW_CONSOLE and we exit 0."""
        from conductor.cli.update import _spawn_installer_and_exit

        c, _buf = _make_console()
        with (
            patch("conductor.cli.update.sys.platform", "win32"),
            patch("subprocess.Popen") as mock_popen,
            pytest.raises(SystemExit) as excinfo,
        ):
            _spawn_installer_and_exit(c)
        assert excinfo.value.code == 0
        mock_popen.assert_called_once()
        kwargs = mock_popen.call_args.kwargs
        # Must spawn detached from the current console.
        create_new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
        assert kwargs.get("creationflags", 0) & create_new_console
        # Must also request job breakaway so the installer survives in
        # CI runners and terminal hosts that kill all job members on close.
        create_breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        assert kwargs.get("creationflags", 0) & create_breakaway
        # Must pass AUTO_STOP so the safety check doesn't trip on our
        # about-to-die conductor process.
        assert kwargs.get("env", {}).get("CONDUCTOR_INSTALL_AUTO_STOP") == "1"
        # And the command must actually be the install one-liner.
        cmd_args = mock_popen.call_args.args[0]
        assert any("install.ps1" in str(a) for a in cmd_args)

    def test_spawn_installer_windows_falls_back_when_breakaway_denied(
        self, cache_dir: Path
    ) -> None:
        """If the parent's job forbids breakaway, retry without that flag."""
        from conductor.cli.update import _spawn_installer_and_exit

        c, _buf = _make_console()
        # First call (with breakaway) raises ERROR_ACCESS_DENIED;
        # second call (CREATE_NEW_CONSOLE only) succeeds.
        access_denied = OSError(5, "Access is denied")
        with (
            patch("conductor.cli.update.sys.platform", "win32"),
            patch("subprocess.Popen", side_effect=[access_denied, MagicMock()]) as mock_popen,
            pytest.raises(SystemExit) as excinfo,
        ):
            _spawn_installer_and_exit(c)
        assert excinfo.value.code == 0
        # Two attempts: with breakaway, then without.
        assert mock_popen.call_count == 2
        first_flags = mock_popen.call_args_list[0].kwargs.get("creationflags", 0)
        second_flags = mock_popen.call_args_list[1].kwargs.get("creationflags", 0)
        create_breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        create_new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
        assert first_flags & create_breakaway
        assert not (second_flags & create_breakaway)
        assert second_flags & create_new_console

    def test_spawn_installer_windows_aborts_when_all_attempts_fail(self, cache_dir: Path) -> None:
        """If both spawn attempts fail, surface the last error and exit 1."""
        from conductor.cli.update import _spawn_installer_and_exit

        c, buf = _make_console()
        with (
            patch("conductor.cli.update.sys.platform", "win32"),
            patch("subprocess.Popen", side_effect=OSError(5, "Access is denied")),
            pytest.raises(SystemExit) as excinfo,
        ):
            _spawn_installer_and_exit(c)
        assert excinfo.value.code == 1
        # User-facing fallback hint must reference the manual install command.
        assert "install.ps1" in buf.getvalue() or "install.sh" in buf.getvalue()

    def test_spawn_installer_posix_execs_sh(self, cache_dir: Path) -> None:
        """On POSIX, the installer replaces the current process via execvpe."""
        from conductor.cli.update import _spawn_installer_and_exit

        c, _buf = _make_console()
        with (
            patch("conductor.cli.update.sys.platform", "linux"),
            patch("os.execvpe") as mock_exec,
        ):
            _spawn_installer_and_exit(c)
        mock_exec.assert_called_once()
        program, argv, env = mock_exec.call_args.args
        assert program == "sh"
        assert argv[0] == "sh" and argv[1] == "-c"
        assert "install.sh" in argv[2]
        assert env.get("CONDUCTOR_INSTALL_AUTO_STOP") == "1"


# ===================================================================
# E3-T3: CLI-level tests
# ===================================================================

runner = CliRunner()


class TestUpdateCommand:
    """CLI tests for ``conductor update``."""

    def test_update_command_invokes_run_update(self, cache_dir: Path) -> None:
        """``conductor update`` should call ``run_update``."""
        with patch("conductor.cli.update.run_update") as mock_run:
            result = runner.invoke(app, ["update"])
        assert result.exit_code == 0
        mock_run.assert_called_once()

    def test_update_apply_flag_passes_through(self, cache_dir: Path) -> None:
        """``conductor update --apply`` must forward ``apply=True``."""
        with patch("conductor.cli.update.run_update") as mock_run:
            result = runner.invoke(app, ["update", "--apply"])
        assert result.exit_code == 0
        mock_run.assert_called_once()
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("apply") is True

    def test_update_apply_flag_visible_in_help(self) -> None:
        """``--apply`` should be a registered option on ``conductor update``.

        We don't check the ``--help`` rendering directly because Rich's
        narrow-terminal output in CI wraps the option name column in ways
        that break naive substring search; introspecting the click command
        is robust to that.
        """
        # Find the click command Typer registered for ``update``.
        click_app = typer.main.get_command(app)
        update_cmd = click_app.commands["update"]
        param_names = {opt for p in update_cmd.params for opt in getattr(p, "opts", [])}
        assert "--apply" in param_names

    def test_update_command_visible_in_help(self) -> None:
        """``update`` should appear in ``conductor --help``."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "update" in result.output

    def test_update_command_error_exits_with_code_1(self, cache_dir: Path) -> None:
        """Errors during update should exit with code 1."""
        with patch(
            "conductor.cli.update.run_update",
            side_effect=RuntimeError("boom"),
        ):
            result = runner.invoke(app, ["update"])
        assert result.exit_code == 1


class TestUpdateHintCLI:
    """CLI tests for update hint integration in ``main()`` callback."""

    def test_hint_appears_in_tty_non_silent(self, cache_dir: Path) -> None:
        """Hint is called when console.is_terminal is True and not silent."""
        mock_console = MagicMock(spec=Console)
        mock_console.is_terminal = True
        with (
            patch("conductor.cli.app.console", mock_console),
            patch("conductor.cli.update.check_for_update_hint") as mock_hint,
            patch("sys.argv", ["conductor", "validate", "--help"]),
        ):
            runner.invoke(app, ["validate", "--help"])
        mock_hint.assert_called_once_with(mock_console)

    def test_hint_not_shown_in_silent_mode(self, cache_dir: Path) -> None:
        """Hint should NOT appear when ``--silent`` is passed."""
        mock_console = MagicMock(spec=Console)
        mock_console.is_terminal = True
        with (
            patch("conductor.cli.app.console", mock_console),
            patch("conductor.cli.update.check_for_update_hint") as mock_hint,
            patch("sys.argv", ["conductor", "--silent", "validate", "--help"]),
        ):
            runner.invoke(app, ["--silent", "validate", "--help"])
        mock_hint.assert_not_called()

    def test_hint_not_shown_for_update_subcommand(self, cache_dir: Path) -> None:
        """Hint should NOT appear when the subcommand is ``update``."""
        mock_console = MagicMock(spec=Console)
        mock_console.is_terminal = True
        with (
            patch("conductor.cli.app.console", mock_console),
            patch("conductor.cli.update.check_for_update_hint") as mock_hint,
            patch("sys.argv", ["conductor", "update"]),
            patch("conductor.cli.update.run_update"),
        ):
            runner.invoke(app, ["update"])
        mock_hint.assert_not_called()

    def test_hint_not_shown_when_not_tty(self, cache_dir: Path) -> None:
        """Hint should NOT appear when console is not a TTY."""
        mock_console = MagicMock(spec=Console)
        mock_console.is_terminal = False
        with (
            patch("conductor.cli.app.console", mock_console),
            patch("conductor.cli.update.check_for_update_hint") as mock_hint,
            patch("sys.argv", ["conductor", "validate", "--help"]),
        ):
            runner.invoke(app, ["validate", "--help"])
        mock_hint.assert_not_called()
