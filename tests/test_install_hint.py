"""Tests for :mod:`conductor.install_hint` — the optional-extra install resolver.

Issue #441: every hint pointing at an optional extra used to hardcode
``pip install 'conductor-cli[<extra>]'``, which cannot work on the documented
install path — a uv tool venv is not pip-managed, and ``conductor-cli`` is not
on PyPI so pip has nothing to resolve against there.

The branch logic lives in the pure :func:`render_install_command`, so every
context is exercised by constructing an :class:`InstallEnvironment` rather than
faking a real install. Detection and receipt parsing are tested separately
against a throwaway prefix.

Two properties get disproportionate attention here because getting them wrong
destroys user state rather than merely printing something unhelpful: the
rendered command must never *drop* an extra the user already has, and this
module must never raise, because it runs while an error message is being
built.
"""

from __future__ import annotations

import json
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from conductor.install_hint import (
    DISTRIBUTION,
    REPO_GIT_URL,
    InstallContext,
    InstallEnvironment,
    ReceiptContents,
    detect_environment,
    extras_spec,
    install_command,
    installed_extras,
    installed_ref,
    read_receipt,
    render_install_command,
    uv_receipt_path,
)

RECEIPT_TUI = """\
[tool]
requirements = [{ name = "conductor-cli", extras = ["tui"], \
git = "https://github.com/microsoft/conductor.git?rev=v0.1.30" }]
entrypoints = [
    { name = "conductor", install-path = "/home/u/.local/bin/conductor", from = "conductor-cli" },
]
"""


def write_receipt(prefix: Path, body: str) -> Path:
    prefix.mkdir(parents=True, exist_ok=True)
    receipt = prefix / "uv-receipt.toml"
    receipt.write_text(body, encoding="utf-8")
    return receipt


@pytest.fixture()
def direct_url(monkeypatch: pytest.MonkeyPatch):
    """Stand in for the installed distribution's PEP 610 ``direct_url.json``."""

    def _set(payload: dict) -> None:
        monkeypatch.setattr("conductor.install_hint._direct_url", lambda: payload)

    return _set


class TestRenderInstallCommand:
    """The pure branch logic — one assertion per detected context."""

    def test_uv_tool_emits_a_pinned_direct_reference(self) -> None:
        env = InstallEnvironment(InstallContext.UV_TOOL, source=f"git+{REPO_GIT_URL}@v0.1.30")

        assert render_install_command("tui", env) == (
            "uv tool install --force "
            "'conductor-cli[tui] @ git+https://github.com/microsoft/conductor.git@v0.1.30'"
        )

    def test_editable_checkout_keeps_other_extras(self) -> None:
        """`uv sync` is exact by default, so without --inexact this command
        removes whatever another extra had installed — the same damage the
        uv-tool branch unions to avoid."""
        assert (
            render_install_command("tui", InstallEnvironment(InstallContext.EDITABLE))
            == "uv sync --inexact --extra tui"
        )

    def test_unknown_install_falls_back_to_pip(self) -> None:
        """Kept as the last-resort fallback: pip resolves an extra against an
        already-installed distribution, which works for a wheel install."""
        env = InstallEnvironment(InstallContext.UNKNOWN)

        assert render_install_command("tui", env).endswith("-m pip install 'conductor-cli[tui]'")

    def test_the_pip_fallback_names_this_interpreter(self) -> None:
        """A bare `pip` is not the pip that manages a pipx venv (or any venv
        this interpreter is not on the PATH of). There it *succeeds*, quietly
        installing a second copy the user never runs, while `conductor fleet`
        keeps printing the same error — a loud failure turned into a silent
        wrong outcome."""
        command = render_install_command("tui", InstallEnvironment(InstallContext.UNKNOWN))

        assert command.startswith(f"{sys.executable} -m pip install")

    def test_unknown_install_from_git_keeps_the_url_it_came_from(self) -> None:
        """A pip/pipx-from-git install is the population that actually lands in
        UNKNOWN, and a bare name genuinely does not resolve for it. The URL is
        recorded in direct_url.json, so putting it back is what makes the
        command work rather than reproducing issue #441."""
        env = InstallEnvironment(InstallContext.UNKNOWN, source=f"git+{REPO_GIT_URL}@v0.1.30")

        assert render_install_command("aca", env).endswith(
            "-m pip install 'conductor-cli[aca] @ "
            "git+https://github.com/microsoft/conductor.git@v0.1.30'"
        )

    def test_uv_tool_preserves_already_installed_extras(self) -> None:
        """`uv tool install --force` rewrites the tool's whole requirement set,
        so a command that named only the requested extra would uninstall the
        ones already there."""
        env = InstallEnvironment(InstallContext.UV_TOOL, frozenset({"tui"}), "git+x@v1")

        assert "conductor-cli[aca,tui] @" in render_install_command("aca", env)

    def test_requesting_an_already_installed_extra_does_not_duplicate_it(self) -> None:
        env = InstallEnvironment(InstallContext.UV_TOOL, frozenset({"tui"}), "git+x@v1")

        assert "conductor-cli[tui] @" in render_install_command("tui", env)

    def test_extras_are_sorted_so_the_command_is_stable(self) -> None:
        env = InstallEnvironment(
            InstallContext.UV_TOOL, frozenset({"tui", "aca", "claude-agent-sdk"}), "git+x@v1"
        )

        assert "conductor-cli[aca,claude-agent-sdk,tui] @" in render_install_command("tui", env)

    def test_the_recorded_install_source_is_reused(self) -> None:
        """Hardcoding the upstream URL would silently redirect a fork or a
        locally-built install at microsoft/conductor's released tag."""
        env = InstallEnvironment(
            InstallContext.UV_TOOL, source="git+https://github.com/me/fork.git@wip"
        )

        assert render_install_command("tui", env).endswith(
            "'conductor-cli[tui] @ git+https://github.com/me/fork.git@wip'"
        )

    def test_an_unreadable_receipt_warns_inside_the_command(self) -> None:
        """An unreadable receipt is not a bare install, and rendering it as one
        produces a confident command that uninstalls the user's extras. The
        caveat is a shell comment, so the line is still safe to paste."""
        env = InstallEnvironment(InstallContext.UV_TOOL, source="git+x@v1", extras_known=False)

        command = render_install_command("tui", env)

        assert "WARNING" in command
        assert "  #" in command

    def test_a_readable_receipt_carries_no_warning(self) -> None:
        env = InstallEnvironment(InstallContext.UV_TOOL, source="git+x@v1")

        assert "WARNING" not in render_install_command("tui", env)

    @pytest.mark.parametrize("context", list(InstallContext))
    def test_every_context_names_the_requested_extra(self, context: InstallContext) -> None:
        """A hint that loses the extra it was raised about is useless
        regardless of which branch produced it."""
        assert "aca" in render_install_command("aca", InstallEnvironment(context))

    def test_no_context_emits_the_dead_pypi_command_for_a_uv_tool_install(self) -> None:
        """The regression this issue is about: a uv tool install can never be
        fixed by `pip install`, because that venv is not pip-managed."""
        env = InstallEnvironment(InstallContext.UV_TOOL, source="git+x@v1")

        assert not render_install_command("tui", env).startswith("pip install")


class TestInstallEnvironment:
    """The invariant lives in the type, not only in its factory."""

    def test_an_editable_environment_cannot_carry_uv_tool_state(self) -> None:
        """`uv sync` names neither extras nor a source, so holding them would
        be state the renderer silently ignores."""
        env = InstallEnvironment(
            InstallContext.EDITABLE,
            frozenset({"tui"}),
            "git+x@v1",
            extras_known=False,
            receipt="/x/uv-receipt.toml",
        )

        assert env.extras == frozenset()
        assert env.source is None
        assert env.extras_known is True
        assert env.receipt is None

    def test_uv_tool_state_is_preserved(self) -> None:
        env = InstallEnvironment(InstallContext.UV_TOOL, frozenset({"tui"}), "git+x@v1")

        assert env.extras == frozenset({"tui"})
        assert env.source == "git+x@v1"


class TestDetectEnvironment:
    """Detection against a throwaway prefix — no real install involved."""

    def test_uv_receipt_means_uv_tool(self, tmp_path: Path, direct_url) -> None:
        direct_url({})
        write_receipt(tmp_path, RECEIPT_TUI)

        env = detect_environment(tmp_path)

        assert env.receipt == str(tmp_path / "uv-receipt.toml")
        assert env.context is InstallContext.UV_TOOL
        assert env.extras == frozenset({"tui"})
        assert env.source == "git+https://github.com/microsoft/conductor.git@v0.1.30"

    def test_editable_direct_url_means_editable(self, tmp_path: Path, direct_url) -> None:
        direct_url({"url": "file:///src/conductor", "dir_info": {"editable": True}})

        assert detect_environment(tmp_path).context is InstallContext.EDITABLE

    def test_neither_signal_is_unknown(self, tmp_path: Path, direct_url) -> None:
        direct_url({})

        assert detect_environment(tmp_path).context is InstallContext.UNKNOWN

    def test_a_non_editable_direct_url_is_unknown(self, tmp_path: Path, direct_url) -> None:
        """A wheel built from a VCS checkout records `direct_url.json` too;
        only `dir_info.editable` means a source checkout."""
        direct_url({"url": "https://x/y.whl", "dir_info": {"editable": False}})

        assert detect_environment(tmp_path).context is InstallContext.UNKNOWN

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            (
                {"url": "file:///tmp/dl/conductor_cli-1.0-py3-none-any.whl", "archive_info": {}},
                None,
            ),
            ({"url": "https://x/conductor_cli-1.0.tar.gz", "archive_info": {}}, None),
            ({"url": "file:///src/conductor", "dir_info": {}}, "file:///src/conductor"),
        ],
        ids=["local-wheel", "remote-sdist", "local-directory"],
    )
    def test_an_artifact_url_is_not_used_as_a_source(
        self, tmp_path: Path, direct_url, payload: dict, expected: str | None
    ) -> None:
        """A wheel or sdist is one artifact, usually a download that has since
        been cleaned up, so pinning to it produces a command that fails with
        "No such file or directory". A bare name resolves against the
        installed distribution instead. A *directory* is still there, so it is
        usable."""
        direct_url(payload)

        assert detect_environment(tmp_path).source == expected

    def test_an_unknown_install_recovers_its_git_source(self, tmp_path: Path, direct_url) -> None:
        direct_url(
            {
                "url": "https://github.com/microsoft/conductor.git",
                "vcs_info": {"vcs": "git", "requested_revision": "v0.1.30"},
            }
        )

        assert (
            detect_environment(tmp_path).source
            == "git+https://github.com/microsoft/conductor.git@v0.1.30"
        )

    def test_the_receipt_wins_over_an_editable_marker(self, tmp_path: Path, direct_url) -> None:
        """A uv tool install writes *both* a receipt and a `direct_url.json`,
        so the receipt has to be checked first or every tool install would be
        misread."""
        direct_url({"dir_info": {"editable": True}})
        write_receipt(tmp_path, RECEIPT_TUI)

        assert detect_environment(tmp_path).context is InstallContext.UV_TOOL

    def test_an_unreadable_receipt_is_reported_as_such(self, tmp_path: Path, direct_url) -> None:
        direct_url({})
        write_receipt(tmp_path, "this is not = valid toml [[[")

        assert detect_environment(tmp_path).extras_known is False

    def test_receipt_path_is_under_the_prefix(self, tmp_path: Path) -> None:
        assert uv_receipt_path(tmp_path) == tmp_path / "uv-receipt.toml"


class TestReadReceipt:
    """Receipt parsing. Never raises: this runs on an error path."""

    def test_reads_a_single_extra_and_its_source(self, tmp_path: Path) -> None:
        write_receipt(tmp_path, RECEIPT_TUI)

        contents = read_receipt(tmp_path)

        assert contents.extras == frozenset({"tui"})
        assert contents.source == "git+https://github.com/microsoft/conductor.git@v0.1.30"
        assert contents.readable is True

    def test_reads_multiple_extras(self, tmp_path: Path) -> None:
        write_receipt(
            tmp_path,
            '[tool]\nrequirements = [{ name = "conductor-cli", extras = ["tui", "aca"] }]\n',
        )

        assert read_receipt(tmp_path).extras == frozenset({"tui", "aca"})

    def test_reads_a_local_directory_source(self, tmp_path: Path) -> None:
        write_receipt(
            tmp_path,
            '[tool]\nrequirements = [{ name = "conductor-cli", directory = "/src/conductor" }]\n',
        )

        assert read_receipt(tmp_path).source == "/src/conductor"

    def test_matches_a_non_canonical_distribution_name(self, tmp_path: Path) -> None:
        """PEP 503 says `conductor_cli` and `conductor-cli` are the same
        project; a literal comparison would silently find no extras and the
        upgrade would drop them."""
        write_receipt(
            tmp_path,
            '[tool]\nrequirements = [{ name = "Conductor_CLI", extras = ["tui"] }]\n',
        )

        assert read_receipt(tmp_path).extras == frozenset({"tui"})

    def test_ignores_other_requirements(self, tmp_path: Path) -> None:
        """`uv tool install --with X` adds requirements that are not this
        distribution; their extras do not belong in the reinstall spec."""
        write_receipt(
            tmp_path,
            "[tool]\nrequirements = [\n"
            '  { name = "other-pkg", extras = ["zzz"] },\n'
            '  { name = "conductor-cli", extras = ["tui"] },\n'
            "]\n",
        )

        assert read_receipt(tmp_path).extras == frozenset({"tui"})

    def test_a_similarly_named_requirement_is_not_mistaken_for_this_one(
        self, tmp_path: Path
    ) -> None:
        write_receipt(
            tmp_path,
            "[tool]\nrequirements = [\n"
            '  { name = "conductor-cli-plugin", extras = ["evil"] },\n'
            '  { name = "conductor-cli", extras = ["tui"] },\n'
            "]\n",
        )

        assert read_receipt(tmp_path).extras == frozenset({"tui"})

    def test_rejects_an_extra_name_that_would_break_the_quoting(self, tmp_path: Path) -> None:
        """These values are interpolated into a single-quoted command the user
        is told to paste."""
        write_receipt(
            tmp_path,
            "[tool]\nrequirements = "
            '[{ name = "conductor-cli", extras = ["tui", "a\'; rm -rf ~; \'"] }]\n',
        )

        assert read_receipt(tmp_path).extras == frozenset({"tui"})

    def test_no_extras_recorded_is_empty_but_readable(self, tmp_path: Path) -> None:
        write_receipt(tmp_path, '[tool]\nrequirements = [{ name = "conductor-cli" }]\n')

        contents = read_receipt(tmp_path)

        assert contents.extras == frozenset()
        assert contents.readable is True

    def test_missing_receipt_is_empty_and_readable(self, tmp_path: Path) -> None:
        """A first-time install has nothing to lose, so this must not render
        the unreadable-receipt warning."""
        assert read_receipt(tmp_path) == ReceiptContents()

    def test_malformed_receipt_is_flagged_unreadable(self, tmp_path: Path) -> None:
        write_receipt(tmp_path, "this is not = valid toml [[[")

        assert read_receipt(tmp_path) == ReceiptContents(readable=False)

    def test_a_non_utf8_receipt_is_flagged_rather_than_raising(self, tmp_path: Path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "uv-receipt.toml").write_bytes(b"\xff\xfe not utf-8")

        assert read_receipt(tmp_path).readable is False

    @pytest.mark.parametrize(
        "body",
        [
            'tool = "not-a-table"\n',
            "[[tool]]\nx = 1\n",
            '[tool]\nrequirements = "not-a-list"\n',
            '[tool]\nrequirements = [{ name = "something-else" }]\n',
        ],
        ids=["tool-is-a-string", "tool-is-an-array", "requirements-not-a-list", "no-entry"],
    )
    def test_unexpected_shapes_are_flagged_rather_than_raising(
        self, tmp_path: Path, body: str
    ) -> None:
        """A future uv could change the receipt schema. Degrading is
        survivable; raising out of an error path is not, and reporting it as a
        bare install would silently drop the user's extras."""
        write_receipt(tmp_path, body)

        assert read_receipt(tmp_path).readable is False

    def test_installed_extras_is_the_extras_half_of_read_receipt(self, tmp_path: Path) -> None:
        write_receipt(tmp_path, RECEIPT_TUI)

        assert installed_extras(tmp_path) == frozenset({"tui"})


class TestDirectUrl:
    """The one function that touches real ``importlib.metadata``."""

    def _distribution(self, monkeypatch: pytest.MonkeyPatch, payload) -> None:
        class _Dist:
            def read_text(self, _name: str):
                if isinstance(payload, Exception):
                    raise payload
                return payload

        monkeypatch.setattr("conductor.install_hint.distribution", lambda _n: _Dist())

    def test_reads_and_parses_the_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from conductor.install_hint import _direct_url

        self._distribution(monkeypatch, json.dumps({"url": "https://x", "dir_info": {}}))

        assert _direct_url()["url"] == "https://x"

    def test_a_missing_distribution_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from conductor.install_hint import _direct_url

        def _raise(_n: str):
            raise PackageNotFoundError(DISTRIBUTION)

        monkeypatch.setattr("conductor.install_hint.distribution", _raise)

        assert _direct_url() == {}

    def test_a_non_utf8_file_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`Distribution.read_text` suppresses a handful of OSError subclasses
        but not UnicodeDecodeError, which is a ValueError — so a corrupt file
        used to escape as a traceback in place of the real error message."""
        from conductor.install_hint import _direct_url

        self._distribution(monkeypatch, UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"))

        assert _direct_url() == {}

    def test_an_absent_file_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from conductor.install_hint import _direct_url

        self._distribution(monkeypatch, None)

        assert _direct_url() == {}

    def test_unparseable_json_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from conductor.install_hint import _direct_url

        self._distribution(monkeypatch, "{not json")

        assert _direct_url() == {}

    def test_non_object_json_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from conductor.install_hint import _direct_url

        self._distribution(monkeypatch, "[1, 2, 3]")

        assert _direct_url() == {}


class TestInstalledRef:
    """The last-resort pin, used only when no source was recorded anywhere."""

    def test_falls_back_to_the_installed_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("conductor.install_hint.version", lambda _dist: "9.9.9")

        assert installed_ref() == "v9.9.9"

    def test_returns_none_when_metadata_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(_dist: str) -> str:
            raise PackageNotFoundError(DISTRIBUTION)

        monkeypatch.setattr("conductor.install_hint.version", _raise)

        assert installed_ref() is None


class TestInstallCommandNeverRaises:
    """The public entry point is called while a ``ProviderError`` is being
    constructed. An exception here does not degrade the hint — it deletes the
    diagnosis the user needed."""

    def test_a_broken_receipt_still_yields_a_command(self, tmp_path: Path, direct_url) -> None:
        direct_url({})
        write_receipt(tmp_path, 'tool = "not-a-table"\n')

        assert install_command("tui", tmp_path)

    def test_an_exploding_detector_still_yields_a_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_prefix=None):
            raise RuntimeError("detection blew up")

        monkeypatch.setattr("conductor.install_hint.detect_environment", _boom)

        command = install_command("tui")

        # The last resort is the install script, and which one depends on the
        # platform — asserting the POSIX form unconditionally would fail on
        # Windows for a command that is correct there.
        script = "install.ps1" if sys.platform == "win32" else "install.sh"
        assert script in command
        assert "tui" in command

    @pytest.mark.parametrize("platform", ["win32", "linux"])
    def test_the_last_resort_names_the_right_installer_per_platform(
        self, monkeypatch: pytest.MonkeyPatch, platform: str
    ) -> None:
        """`curl … | sh` is not runnable in PowerShell, so a POSIX-only last
        resort would leave a Windows user with no working command at all —
        the failure mode this module exists to remove."""

        def _boom(_prefix=None):
            raise RuntimeError("detection blew up")

        monkeypatch.setattr("conductor.install_hint.detect_environment", _boom)
        monkeypatch.setattr(sys, "platform", platform)

        command = install_command("aca")

        expected = "install.ps1" if platform == "win32" else "install.sh"
        assert expected in command
        assert "aca" in command

    def test_composes_detection_and_rendering(self, tmp_path: Path, direct_url) -> None:
        direct_url({})
        write_receipt(tmp_path, RECEIPT_TUI)

        assert install_command("aca", tmp_path) == (
            "uv tool install --force "
            "'conductor-cli[aca,tui] @ git+https://github.com/microsoft/conductor.git@v0.1.30'"
        )


class TestExtrasSpec:
    def test_sorts_and_names_the_distribution(self) -> None:
        assert extras_spec({"tui", "aca"}) == "conductor-cli[aca,tui]"

    def test_uses_the_distribution_name_not_the_command_name(self) -> None:
        """The command is `conductor`; the distribution is `conductor-cli`.
        Naming the command here produces a spec that resolves to an unrelated
        project."""
        assert extras_spec({"tui"}).startswith(f"{DISTRIBUTION}[")


class TestDeclaredExtrasAreReal:
    """A typo in an extra name reproduces #441 exactly — a confidently printed
    command that installs nothing."""

    @pytest.mark.parametrize("extra", ["tui", "aca", "claude-agent-sdk", "telemetry"])
    def test_the_extra_is_declared_by_this_package(self, extra: str) -> None:
        import tomllib

        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
            "optional-dependencies"
        ]

        assert extra in declared
