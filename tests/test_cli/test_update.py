"""Tests for update check and self-upgrade utilities (``conductor.cli.update``).

Covers:
- Cache path, read/write, and expiry logic
- Version parsing, pre-release detection, and comparison
- ``fetch_latest_version()`` with mocked network responses
- ``check_for_update_hint()`` with TTY/verbosity/subcommand guards
- ``run_update()`` with mocked subprocess execution
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
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
# E2-T8: run_update
# ===================================================================


class TestRunUpdate:
    """Tests for ``run_update`` with mocked subprocess."""

    def test_successful_upgrade(self, cache_dir: Path) -> None:
        """Successful upgrade prints before/after and clears cache."""
        # Pre-populate cache to verify it's deleted
        cache_file = cache_dir / "update-check.json"
        cache_file.write_text("{}")

        c, buf = _make_console(is_terminal=True)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""

        with (
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("99.0.0", "v99.0.0", "https://example.com"),
            ),
            patch("conductor.cli.update.subprocess.run", return_value=mock_proc) as mock_run,
        ):
            run_update(c)

        output = buf.getvalue()
        assert "99.0.0" in output
        assert "Successfully upgraded" in output

        # Verify subprocess was called with the correct command
        args = mock_run.call_args[0][0]
        assert args[0] == "uv"
        assert "--force" in args
        assert any("@v99.0.0" in a for a in args)

        # Cache should be deleted
        assert not cache_file.exists()

    def test_already_up_to_date(self, cache_dir: Path) -> None:
        """When local == remote, should say 'already up to date'."""
        import conductor

        c, buf = _make_console(is_terminal=True)
        with patch(
            "conductor.cli.update.fetch_latest_version",
            return_value=(
                conductor.__version__,
                f"v{conductor.__version__}",
                "https://example.com",
            ),
        ):
            run_update(c)

        output = buf.getvalue()
        assert "Already up to date" in output

    def test_upgrade_failure(self, cache_dir: Path) -> None:
        """Failed subprocess should report the error."""
        c, buf = _make_console(is_terminal=True)
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "some error output"

        with (
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("99.0.0", "v99.0.0", "https://example.com"),
            ),
            patch("conductor.cli.update.subprocess.run", return_value=mock_proc),
        ):
            run_update(c)

        output = buf.getvalue()
        assert "Upgrade failed" in output

    def test_network_failure(self, cache_dir: Path) -> None:
        """When fetch fails, should print an error."""
        c, buf = _make_console(is_terminal=True)
        with patch("conductor.cli.update.fetch_latest_version", return_value=None):
            run_update(c)

        output = buf.getvalue()
        assert "Could not reach GitHub" in output

    def test_command_includes_tag_name(self, cache_dir: Path) -> None:
        """The subprocess command must include ``@{tag_name}``."""
        c, buf = _make_console(is_terminal=True)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""

        with (
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("2.0.0", "v2.0.0", "https://example.com"),
            ),
            patch("conductor.cli.update.subprocess.run", return_value=mock_proc) as mock_run,
        ):
            run_update(c)

        args = mock_run.call_args[0][0]
        install_arg = [a for a in args if a.startswith("git+")]
        assert len(install_arg) == 1
        assert install_arg[0].endswith("@v2.0.0")

    def test_windows_renames_exe_before_install(self, cache_dir: Path, tmp_path: Path) -> None:
        """On Windows, ``run_update`` renames the exe to ``.exe.old`` before calling ``uv``."""
        # Create a fake conductor.exe
        fake_exe = tmp_path / "conductor.exe"
        fake_exe.write_text("fake")

        cache_file = cache_dir / "update-check.json"
        cache_file.write_text("{}")

        c, buf = _make_console(is_terminal=True)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""

        with (
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("99.0.0", "v99.0.0", "https://example.com"),
            ),
            patch("conductor.cli.update.sys.platform", "win32"),
            patch("conductor.cli.update._get_conductor_exe", return_value=fake_exe),
            patch("conductor.cli.update.subprocess.run", return_value=mock_proc) as mock_run,
        ):
            run_update(c)

        # The exe should have been renamed to .exe.old
        old_exe = tmp_path / "conductor.exe.old"
        assert old_exe.exists()

        # subprocess.run must be called (not Popen)
        mock_run.assert_called_once()

        # Output should say successful (synchronous path)
        output = buf.getvalue()
        assert "Successfully upgraded" in output

        # Cache should be cleared
        assert not cache_file.exists()

    def test_windows_restores_exe_on_failure(self, cache_dir: Path, tmp_path: Path) -> None:
        """On Windows, if ``uv`` fails and doesn't write a new exe, the old one is restored."""
        # Create a fake conductor.exe
        fake_exe = tmp_path / "conductor.exe"
        fake_exe.write_text("fake")

        c, buf = _make_console(is_terminal=True)
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "install failed"

        with (
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("99.0.0", "v99.0.0", "https://example.com"),
            ),
            patch("conductor.cli.update.sys.platform", "win32"),
            patch("conductor.cli.update._get_conductor_exe", return_value=fake_exe),
            patch("conductor.cli.update.subprocess.run", return_value=mock_proc),
        ):
            run_update(c)

        # The .old file should have been renamed back to .exe since uv didn't write a new one
        assert fake_exe.exists()
        old_exe = tmp_path / "conductor.exe.old"
        assert not old_exe.exists()

        # Output should report failure
        output = buf.getvalue()
        assert "Upgrade failed" in output

    def test_windows_cleans_up_previous_old_exe(self, cache_dir: Path, tmp_path: Path) -> None:
        """On Windows, a leftover ``.exe.old`` from a previous update is deleted first."""
        # Create a fake conductor.exe and a pre-existing .old file
        fake_exe = tmp_path / "conductor.exe"
        fake_exe.write_text("new-fake")
        old_leftover = tmp_path / "conductor.exe.old"
        old_leftover.write_text("stale-old")

        c, buf = _make_console(is_terminal=True)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""

        with (
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("99.0.0", "v99.0.0", "https://example.com"),
            ),
            patch("conductor.cli.update.sys.platform", "win32"),
            patch("conductor.cli.update._get_conductor_exe", return_value=fake_exe),
            patch("conductor.cli.update.subprocess.run", return_value=mock_proc),
        ):
            run_update(c)

        # The old leftover should be gone, replaced by the newly renamed exe
        old_exe = tmp_path / "conductor.exe.old"
        assert old_exe.exists()
        # The content should be "new-fake" (current exe), not "stale-old"
        assert old_exe.read_text() == "new-fake"

    def test_unix_skips_rename(self, cache_dir: Path) -> None:
        """On non-Windows platforms, ``_get_conductor_exe`` is not called."""
        c, buf = _make_console(is_terminal=True)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""

        with (
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("99.0.0", "v99.0.0", "https://example.com"),
            ),
            patch("conductor.cli.update.sys.platform", "linux"),
            patch("conductor.cli.update.subprocess.run", return_value=mock_proc) as mock_run,
            patch("conductor.cli.update._get_conductor_exe") as mock_get_exe,
        ):
            run_update(c)

        # _get_conductor_exe must NOT be called on Linux
        mock_get_exe.assert_not_called()

        # subprocess.run must be called
        mock_run.assert_called_once()

        # Output should mention successful upgrade
        output = buf.getvalue()
        assert "Successfully upgraded" in output

    def test_windows_entrypoint_failure_reports_success(
        self, cache_dir: Path, tmp_path: Path
    ) -> None:
        """On Windows, if uv installs the package but fails to copy the entrypoint,
        report success with a restart note instead of failure."""
        fake_exe = tmp_path / "conductor.exe"
        fake_exe.write_text("fake")

        cache_file = cache_dir / "update-check.json"
        cache_file.write_text("{}")

        c, buf = _make_console(is_terminal=True)
        mock_proc = MagicMock()
        mock_proc.returncode = 2
        mock_proc.stderr = "error: Failed to install entrypoint\n  Caused by: failed to copy file"

        with (
            patch(
                "conductor.cli.update.fetch_latest_version",
                return_value=("99.0.0", "v99.0.0", "https://example.com"),
            ),
            patch("conductor.cli.update.sys.platform", "win32"),
            patch("conductor.cli.update._get_conductor_exe", return_value=fake_exe),
            patch("conductor.cli.update.subprocess.run", return_value=mock_proc),
        ):
            run_update(c)

        output = buf.getvalue()
        assert "Successfully upgraded" in output
        assert "restart your terminal" in output
        assert "Upgrade failed" not in output

        # Cache should be cleared on partial success
        assert not cache_file.exists()


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
