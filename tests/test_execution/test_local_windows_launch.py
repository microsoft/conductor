"""Declared-context Windows launch policy and native platform contracts."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from conductor.execution import local
from conductor.execution import local_process_scope as process_core
from conductor.execution import local_windows_launch as windows
from conductor.execution.errors import ExecutionSpecError
from conductor.execution.local_process_scope import LocalProcessScopeController
from conductor.execution.types import CommandResult, CommandSpec, ProcessLaunchSpec, WorkspaceLease

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Native Windows kernel required")
REFUSALS = [
    pytest.param(value, id=name)
    for name, value in [
        ("quote", '"'),
        ("percent", "%"),
        ("delayed-expansion", "!"),
        ("caret", "^"),
        ("ampersand", "&"),
        ("pipe", "|"),
        ("input-redirection", "<"),
        ("output-redirection", ">"),
        ("left-parenthesis", "("),
        ("right-parenthesis", ")"),
        ("star", "*"),
        ("question", "?"),
        ("cr", "\r"),
        ("lf", "\n"),
        ("tab", "\t"),
        ("nul", "\0"),
        ("del", "\x7f"),
        ("c1", "\x85"),
        ("surrogate", "\ud800"),
        ("line-separator", "\u2028"),
        ("paragraph-separator", "\u2029"),
        ("quoted-trailing-backslash", "C:\\with space\\"),
    ]
]


@pytest.mark.parametrize(
    ("platform", "managed", "lookups"),
    [("win32", True, 0), ("win32", False, 1), ("linux", True, 1)],
)
async def test_backend_avoids_only_discarded_managed_windows_lookup(
    monkeypatch: pytest.MonkeyPatch, platform: str, managed: bool, lookups: int
) -> None:
    # Requirement: only managed Windows bypasses the discarded ambient which before dispatch.
    backend = local.LocalRunnerBackend()
    spec = CommandSpec("fixture", ("literal",))
    lease = WorkspaceLease("fixture", "local", "inc", "/owned") if managed else None
    managed_launch = AsyncMock(return_value=CommandResult("completed", exit_code=0))
    monkeypatch.setattr(backend, "_run_managed", managed_launch)
    monkeypatch.setattr(local, "sys", SimpleNamespace(platform=platform), raising=False)
    which = Mock(return_value="/ambient/fixture")
    monkeypatch.setattr(local.shutil, "which", which)
    process = Mock(returncode=0)
    process.communicate = AsyncMock(return_value=(b"", b""))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(local.asyncio, "create_subprocess_exec", spawn)

    result = await backend.run_command(spec, lease)

    assert result.outcome == "completed" and result.exit_code == 0
    assert which.call_count == lookups
    if managed:
        managed_launch.assert_awaited_once_with(spec, lease, None, None)
        spawn.assert_not_awaited()
    else:
        assert spawn.await_args is not None
        assert spawn.await_args.args == ("/ambient/fixture", "literal")
        managed_launch.assert_not_awaited()


def test_environment_overlay_preserves_values_and_canonicalizes_sdk_subset() -> None:
    # Requirement: a second SDK merge cannot reintroduce a differently cased inherited value.
    from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS

    base = {name: f"base-{index}" for index, name in enumerate(DEFAULT_INHERITED_ENV_VARS)}
    overlay = {name.lower(): f"overlay-{index}" for index, name in enumerate(base)}
    overlay["Marker"] = "literal \u00e9 \U0001f642"
    merged = windows.merge_windows_environment(
        base, overlay, canonical_names=DEFAULT_INHERITED_ENV_VARS
    )
    expected = {name: f"overlay-{index}" for index, name in enumerate(DEFAULT_INHERITED_ENV_VARS)}
    expected["Marker"] = "literal \u00e9 \U0001f642"
    assert merged == expected
    assert {**base, **merged} == expected
    assert overlay["Marker"] == "literal \u00e9 \U0001f642"
    assert all(base[name] == f"base-{index}" for index, name in enumerate(base))


@pytest.mark.parametrize("side", ["base", "overlay"])
@pytest.mark.parametrize("equal", [True, False])
def test_environment_same_mapping_aliases_are_unambiguous(side: str, equal: bool) -> None:
    # Requirement: equality may collapse aliases; conflicting values in one mapping must refuse.
    values = {"Path": "first", "pATH": "first" if equal else "second"}
    base, overlay = (values, {}) if side == "base" else ({}, values)
    if equal:
        assert windows.merge_windows_environment(base, overlay) == {"PATH": "first"}
    else:
        with pytest.raises(ExecutionSpecError, match="Conflicting Windows environment aliases"):
            windows.merge_windows_environment(base, overlay)
    assert len(values) == 2


@pytest.mark.parametrize("name", ["CONDUCTOR_LOCAL_SCOPE_TOKEN", "conductor_local_scope_token"])
def test_reserved_environment_names_cannot_change_case_to_bypass_admission(name: str) -> None:
    # Requirement: private environment names remain forbidden under Windows name identity.
    with pytest.raises(ExecutionSpecError, match="Reserved"):
        windows.merge_windows_environment({}, {name: "not-a-grant"})


@pytest.mark.parametrize("extension", [".exe", ".com"])
def test_explicit_native_preparation_preserves_arguments_and_defers_os_checks(
    monkeypatch: pytest.MonkeyPatch, extension: str
) -> None:
    # Requirement: explicit native images keep CRT argv and real loader errors, without pre-stat.
    stat = Mock(side_effect=AssertionError("Native image must reach CreateProcess"))
    monkeypatch.setattr(windows, "os", SimpleNamespace(stat=stat))
    env = {"Path": r"C:\declared", "LONG_VALUE": "x" * 9000}
    arguments = ('"quoted"', "%!^&|<>()*?", "C:\\with space\\", "\n", "", "x" * 9000)
    launch = ProcessLaunchSpec(
        (r".\bin\fixture" + extension, *arguments), env, r"C:\work", "unchanged-token"
    )

    prepared, error = windows.prepare_windows_launch(launch)

    assert error is None
    assert prepared.argv == (r"C:\work\bin\fixture" + extension, *arguments)
    assert (
        prepared.control_token == "unchanged-token" and prepared.working_dir == launch.working_dir
    )
    assert prepared.env == {"PATH": r"C:\declared", "LONG_VALUE": "x" * 9000}
    assert prepared.env is not env and env["Path"] == r"C:\declared"
    assert windows.windows_create_process_arguments(prepared) == (
        None,
        subprocess.list2cmdline((r"C:\work\bin\fixture" + extension, *arguments)),
    )
    stat.assert_not_called()


def test_batch_encoder_has_fixed_switches_and_literal_backslash_representation() -> None:
    # Requirement: cmd receives one outer pair, literal empty/Unicode arguments and no CRT escapes.
    launch = ProcessLaunchSpec(
        (
            r"C:\work space\literal.cmd",
            "",
            "a b",
            "C:\\plain\\",
            r"C:\work dir\file",
            "\u00e9",
            "\U0001f642",
        ),
        {"ComSpec": r"C:\Windows\System32\cmd.exe"},
        r"C:\work",
    )
    assert windows.windows_create_process_arguments(launch) == (
        r"C:\Windows\System32\cmd.exe",
        '"C:\\Windows\\System32\\cmd.exe" /d /e:on /v:off /s /c '
        '""C:\\work space\\literal.cmd" "" "a b" C:\\plain\\ '
        '"C:\\work dir\\file" "\u00e9" "\U0001f642""',
    )


@pytest.mark.parametrize("name", ["COMSPEC", "SYSTEMROOT"])
def test_invalid_interpreter_nul_cannot_enter_a_batch_command_line(name: str) -> None:
    # Requirement: an unrepresentable interpreter is a policy refusal before filesystem I/O.
    launch = ProcessLaunchSpec((r"C:\literal.cmd",), {name: "C:\\Windows\0"}, r"C:\work")
    with pytest.raises(ExecutionSpecError):
        windows.windows_create_process_arguments(launch)


@pytest.mark.parametrize("value", REFUSALS)
def test_batch_refusal_retains_selected_target_without_echoing_value(value: str) -> None:
    # Requirement: a refusal after resolution retains wrapper metadata and typed, value-free data.
    prepared, error = windows.prepare_windows_launch(
        ProcessLaunchSpec(
            (r".\literal.cmd", value),
            {"COMSPEC": r"C:\Windows\System32\cmd.exe"},
            r"C:\work",
            "retained-token",
        )
    )
    assert prepared.argv == (r"C:\work\literal.cmd", value)
    assert prepared.control_token == "retained-token"
    assert error is not None and error.kind == "os_error"
    assert error.errno is None and error.filename is None
    assert error.message in (
        "Unsupported literal batch character in argument 1",
        "Batch argument 1 needs quoting before a trailing backslash",
    )


@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        (r"C:tool.exe", r"C:\work"),
        (r"\tool.exe", r"C:\work"),
        (r"C:\tool.exe", r"\work"),
        ("", r"C:\work"),
        ("tool.ps1", r"C:\work"),
    ],
)
def test_ambiguous_context_and_associations_refuse(command: str, cwd: str) -> None:
    # Requirement: incomplete paths and associations cannot trigger ambient search or a shell.
    prepared, error = windows.prepare_windows_launch(ProcessLaunchSpec((command,), {}, cwd))
    assert prepared.argv == (command,)
    assert error is not None and error.kind == "os_error" and error.errno is None


@pytest.mark.parametrize("path", [None, ""])
def test_missing_or_empty_path_performs_zero_search(
    monkeypatch: pytest.MonkeyPatch, path: str | None
) -> None:
    # Requirement: absence is authoritative; neither cwd, os.defpath nor parent PATH is searched.
    stat = Mock(side_effect=AssertionError("There are no declared search directories"))
    monkeypatch.setattr(windows, "os", SimpleNamespace(stat=stat, strerror=os.strerror))
    env = {} if path is None else {"PATH": path}
    _prepared, error = windows.prepare_windows_launch(ProcessLaunchSpec(("tool",), env, r"C:\work"))
    assert error is not None and error.kind == "file_not_found"
    assert error.errno == errno.ENOENT and error.filename is None
    stat.assert_not_called()


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("non_bmp", [False, True])
def test_batch_command_limit_counts_utf16_terminator(extra: int, non_bmp: bool) -> None:
    # Requirement: the whole cmd string, including its terminator, fits exactly 8191 UTF-16 units.
    prefix = '"C:\\Windows\\System32\\cmd.exe" /d /e:on /v:off /s /c ""C:\\x.cmd" "'
    available = 8191 - len(prefix) - len('""') - 1 + extra
    argument = (
        "\U0001f642" * (available // 2) + "x" * (available % 2) if non_bmp else "x" * available
    )
    launch = ProcessLaunchSpec(
        (r"C:\x.cmd", argument), {"COMSPEC": r"C:\Windows\System32\cmd.exe"}, r"C:\work"
    )
    if extra:
        with pytest.raises(ExecutionSpecError, match="command line exceeds"):
            windows.windows_create_process_arguments(launch)
    else:
        assert windows.windows_create_process_arguments(launch)[1] == prefix + argument + '""'


@pytest.mark.parametrize("extra", [0, 1])
def test_batch_environment_limit_keeps_every_entry(extra: int) -> None:
    # Requirement: an overlong entry is refused, never dropped; native launches keep it intact.
    env = {"COMSPEC": r"C:\Windows\System32\cmd.exe", "X": "x" * (8188 + extra)}
    launch = ProcessLaunchSpec((r"C:\x.cmd",), env, r"C:\work")
    if extra:
        with pytest.raises(ExecutionSpecError, match="environment entry exceeds"):
            windows.windows_create_process_arguments(launch)
    else:
        assert windows.windows_create_process_arguments(launch)[0] == env["COMSPEC"]
    assert env["X"] == "x" * (8188 + extra)


@pytest.mark.skipif(sys.platform != "linux", reason="Real Linux no-GO wiring control")
async def test_parent_policy_refusal_obtains_real_empty_proof_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: parent policy refusal uses real REJECTED cleanup, not cancellation or fake proof.
    controller = LocalProcessScopeController(str(tmp_path), "policy-wiring")
    dispatch = Mock()
    sender = Mock(wraps=controller._send)
    pump = Mock(wraps=controller._script_input)
    monkeypatch.setattr(controller, "_send", sender)
    monkeypatch.setattr(controller, "_script_input", pump)
    try:
        with monkeypatch.context() as platform:
            platform.setattr(process_core, "sys", SimpleNamespace(platform="win32"))
            result = await asyncio.wait_for(
                controller.spawn_script(
                    "refused",
                    CommandSpec(
                        r"C:\literal.cmd",
                        ('"unsupported"',),
                        r"C:\work",
                        {"COMSPEC": r"C:\Windows\System32\cmd.exe"},
                        stdin=b"must-not-pump",
                        inherit_control_environment=False,
                    ),
                    on_dispatch=dispatch,
                ),
                10,
            )
        assert result.outcome == "start_failed" and result.resolved_command == r"C:\literal.cmd"
        assert result.start_error is not None and result.start_error.errno is None
        dispatch.assert_not_called()
        pump.assert_not_called()
        sent = [call.args[2]["event"] for call in sender.call_args_list]
        assert "GO" not in sent and sent.count("REJECTED") == 1
        assert (await controller.await_release()).status == "released"
        (proof,) = controller.proofs
        assert proof.events == ("CUSTODIAN_READY", "SCOPE_EMPTY", "STREAMS_DRAINED", "DONE")
        assert proof.kernel_empty and proof.streams_drained and proof.custodian_reaped
    finally:
        await controller.close()
        controller._accepting.join(timeout=3)
        assert not controller._accepting.is_alive()


def _copy_windows_image(directory: Path, name: str = "python.exe") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    base = Path(sys.base_prefix)
    original = base / "python.exe"
    assert original.is_file(), "A base Python PE is a mandatory Windows fixture prerequisite"
    dlls = list(base.glob("*.dll"))
    assert any(path.name.lower().startswith("python") for path in dlls)
    target = directory / name
    for source, destination in [
        (original, target),
        *((path, directory / path.name) for path in dlls),
    ]:
        shutil.copyfile(source, destination)
        assert (
            hashlib.sha256(destination.read_bytes()).digest()
            == hashlib.sha256(source.read_bytes()).digest()
        )
    return target


@dataclass(frozen=True, slots=True)
class WindowsFixture:
    root: Path
    work: Path
    image: Path
    peer: Path
    receipts: Path
    env: dict[str, str]


@pytest.fixture
def native(tmp_path: Path) -> WindowsFixture:
    root = tmp_path / "native"
    work = root / "work"
    receipts = root / "receipts"
    home = root / "home"
    for directory in (work, receipts, home):
        directory.mkdir(parents=True)
    image = _copy_windows_image(root / "images")
    peer = root / "peer.py"
    shutil.copyfile(Path(__file__).with_name("windows_launch_fixture.py"), peer)
    env = {
        "SYSTEMROOT": os.environ["SYSTEMROOT"],
        "COMSPEC": os.environ["COMSPEC"],
        "PATH": str(image.parent),
        "PATHEXT": ".EXE;.CMD;.BAT;.COM",
        "PYTHONHOME": sys.base_prefix,
        "PYTHONPATH": os.pathsep.join(
            (
                sysconfig.get_path("purelib"),
                str(Path(local.__file__).parents[2]),
            )
        ),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(root / "pycache"),
        "PYTHONUTF8": "1",
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "TMP": str(root),
        "TEMP": str(root),
        "WINDOWS_PEER_MODE": "record",
        "WINDOWS_PEER_RECEIPTS": str(receipts),
        "WINDOWS_PEER_PYTHON": str(image),
        "WINDOWS_PEER_SCRIPT": str(peer),
        "WINDOWS_PEER_MARKER": "declared",
    }
    bootstrap = subprocess.run(
        [str(image), "-B", str(peer), "bootstrap"],
        env=env,
        cwd=work,
        input=b"bootstrap\0\r\n",
        capture_output=True,
        check=True,
        timeout=10,
    )
    observed = json.loads(bootstrap.stdout)
    assert observed["argv"] == ["bootstrap"] and observed["stdin_hex"] == b"bootstrap\0\r\n".hex()
    assert Path(observed["executable"]) == image
    return WindowsFixture(root, work, image, peer, receipts, env)


def _wrapper(path: Path, *, peer: bool = True, tag: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    command = '"%WINDOWS_PEER_PYTHON%"'
    if peer:
        command += ' -B -u "%WINDOWS_PEER_SCRIPT%"'
    text = "@echo off\r\n"
    if tag:
        text += f'set "WINDOWS_PEER_MARKER={tag}"\r\n'
    path.write_bytes((text + command + " %*\r\n").encode("ascii"))
    return path


async def _join_callbacks(controller: LocalProcessScopeController) -> None:
    while controller._tasks or controller.owners.tasks:
        cohort = {*controller._tasks, *(entry.task for entry in controller.owners.tasks.values())}
        callbacks: list[asyncio.Future[None]] = []
        for owner in cohort:
            assert isinstance(owner, asyncio.Future)
            finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            owner.add_done_callback(lambda _done, finished=finished: finished.set_result(None))
            callbacks.append(finished)
        await asyncio.gather(*callbacks)


@asynccontextmanager
async def _native_controller(root: Path) -> AsyncIterator[LocalProcessScopeController]:
    controller = LocalProcessScopeController(str(root), "native-windows")
    try:
        async with asyncio.timeout(20):
            yield controller
    finally:
        assert (await controller.close()).status == "released"
        await _join_callbacks(controller)
        controller._accepting.join(timeout=3)
        assert not controller._accepting.is_alive()
        assert not controller._tasks and not controller.owners.tasks


@WINDOWS
@pytest.mark.parametrize("extension", [".cmd", ".bat"])
@pytest.mark.parametrize("inherit", [False, True])
async def test_windows_literal_wrapper_argv_unicode_and_script_environment(
    native: WindowsFixture, monkeypatch: pytest.MonkeyPatch, extension: str, inherit: bool
) -> None:
    # Requirement: both batch formats deliver exact literal argv and script overlay precedence.
    wrapper = _wrapper(native.work / "space \u7a7a" / ("literal" + extension))
    arguments = ("", "a b", "C:\\plain\\", r"C:\with space\file", "\u00e9", "\U0001f642")
    monkeypatch.setenv("PATH", r"C:\not-the-declared-path")
    monkeypatch.setenv("WINDOWS_PEER_MARKER", "parent")
    env = {**native.env, "pythonutf8": "0"}
    env.pop("PYTHONUTF8")
    async with _native_controller(native.root) as controller:
        result = await controller.spawn_script(
            "literal",
            CommandSpec(
                str(wrapper),
                arguments,
                str(native.work),
                env,
                stdin=b"\0raw\r\n",
                inherit_control_environment=inherit,
            ),
        )
        assert result.outcome == "completed" and result.exit_code == 0
        assert result.resolved_command == str(wrapper)
        observed = json.loads(result.stdout)
        assert observed["argv"] == list(arguments) and observed["argc"] == len(arguments)
        assert observed["cwd"] == str(native.work) and observed["marker"] == "declared"
        assert observed["pythonutf8"] == "0" and observed["path"] == native.env["PATH"]
        assert observed["stdin_hex"] == b"\0raw\r\n".hex()
        assert observed["in_job"]


@WINDOWS
@pytest.mark.parametrize("value", REFUSALS)
async def test_windows_policy_refusal_never_dispatches_payload(
    native: WindowsFixture, value: str
) -> None:
    # Requirement: every unsupported class refuses before GO with real empty/drain/reap receipts.
    wrapper = _wrapper(native.work / "refused.cmd")
    before = set(native.receipts.iterdir())
    dispatch = Mock()
    async with _native_controller(native.root) as controller:
        result = await controller.spawn_script(
            "policy",
            CommandSpec(
                str(wrapper),
                (value,),
                str(native.work),
                native.env,
                stdin=b"not-delivered",
                inherit_control_environment=False,
            ),
            on_dispatch=dispatch,
        )
        assert result.outcome == "start_failed" and result.start_error is not None
        assert result.start_error.kind == "os_error"
        assert result.start_error.errno is None and result.start_error.filename is None
        assert result.resolved_command == str(wrapper)
        dispatch.assert_not_called()
        assert set(native.receipts.iterdir()) == before
        assert (await controller.await_release()).status == "released"
        (proof,) = controller.proofs
        assert proof.events == ("CUSTODIAN_READY", "SCOPE_EMPTY", "STREAMS_DRAINED", "DONE")
        assert proof.kernel_empty and proof.streams_drained and proof.custodian_reaped


@WINDOWS
@pytest.mark.parametrize(
    ("mode", "pathext", "expected"),
    [
        ("declared", None, "tool.COM"),
        ("declared", "", None),
        ("declared", ".CMD;.EXE", "tool.CMD"),
        ("declared", ".EXE;.CMD", "tool.EXE"),
        ("declared", ".PS1;.EXE", "tool.EXE"),
        ("declared", ".PS1;.JS", None),
        ("directory-order", ".EXE;.COM", "tool.COM"),
        ("missing-path", None, None),
        ("empty-path", None, None),
        ("relative", ".EXE", "tool.EXE"),
        ("dot", ".EXE", "tool.EXE"),
        ("quoted", ".EXE", "tool.EXE"),
        ("explicit-suffix", ".CMD", "tool.EXE"),
        ("qualified-extensionless", ".EXE", "tool.EXE"),
    ],
)
async def test_windows_declared_search_selects_actual_image(
    native: WindowsFixture,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    pathext: str | None,
    expected: str | None,
) -> None:
    # Requirement: only final PATH/PATHEXT and cwd select the image, in directory-outer order.
    parent = _copy_windows_image(native.root / "parent", "tool.EXE")
    first = native.work if mode == "dot" else native.work / "bin"
    second = native.root / "second"
    _copy_windows_image(first, "tool.COM")
    if mode != "directory-order":
        _copy_windows_image(first, "tool.EXE")
    _copy_windows_image(second, "tool.EXE")
    _wrapper(first / "tool.CMD", peer=False, tag="batch")
    _wrapper(first / "tool.EXE.CMD", peer=False, tag="wrong-suffix")
    monkeypatch.setenv("PATH", str(parent.parent))
    monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.chdir(parent.parent)
    env = {**native.env, "PATH": f"{first};{second}"}
    env.pop("PATHEXT")
    if pathext is not None:
        env["PATHEXT"] = pathext
    match mode:
        case "missing-path":
            env.pop("PATH")
        case "empty-path":
            env["PATH"] = ""
        case "relative":
            env["PATH"] = "bin"
        case "dot":
            env["PATH"] = "."
        case "quoted":
            env["PATH"] = f'"{first}"'
    command = "tool.EXE" if mode == "explicit-suffix" else "tool"
    if mode == "qualified-extensionless":
        command = r"bin\tool"
        env["PATH"] = str(parent.parent)
    async with _native_controller(native.root) as controller:
        result = await controller.spawn_script(
            "search",
            CommandSpec(
                command,
                ("-B", str(native.peer), "search"),
                str(native.work),
                env,
                stdin=b"",
                inherit_control_environment=False,
            ),
        )
        if expected is None:
            assert result.outcome == "command_not_found" and result.start_error is not None
            assert result.start_error.errno == errno.ENOENT and result.start_error.filename is None
            assert all("COMMAND_STARTED" not in proof.events for proof in controller.proofs)
        else:
            assert result.outcome == "completed" and result.exit_code == 0
            assert result.resolved_command == str(first / expected)
            observed = json.loads(result.stdout)
            assert observed["argv"] == ["search"]
            selected = native.image if expected == "tool.CMD" else first / expected
            assert Path(observed["executable"]) == selected
            assert observed["marker"] == ("batch" if expected == "tool.CMD" else "declared")


@WINDOWS
def test_windows_ordinal_environment_identity_is_not_unicode_casefold() -> None:
    # Requirement: native ordinal comparison collapses a one-letter alias, not multi-letter folds.
    assert windows.merge_windows_environment({"\u00e9": "base"}, {"\u00c9": "overlay"}) == {
        "\u00c9": "overlay"
    }
    assert windows.merge_windows_environment({"\u00df": "one", "SS": "two"}, {}) == {
        "\u00df": "one",
        "SS": "two",
    }
    with pytest.raises(ExecutionSpecError, match="Conflicting"):
        windows.merge_windows_environment({"\u00e9": "first", "\u00c9": "second"}, {})
    assert windows.merge_windows_environment(
        {"\U0001f600": "first", "\U0001f642": "second"}, {}
    ) == {"\U0001f600": "first", "\U0001f642": "second"}


@WINDOWS
@pytest.mark.parametrize("extension", [".exe", ".com"])
async def test_windows_native_images_keep_cmd_unsupported_argv_and_long_environment(
    native: WindowsFixture, extension: str
) -> None:
    # Requirement: the automatic batch subset never restricts native CRT argv or environment.
    image = _copy_windows_image(native.root / "native-parity", "fixture" + extension)
    arguments = (
        '"quoted"',
        "%!^&|<>()*?",
        "C:\\with space\\",
        "\n\t",
        "",
        "\U0001f642",
        "x" * 9000,
    )
    async with _native_controller(native.root) as controller:
        result = await controller.spawn_script(
            "native",
            CommandSpec(
                str(image),
                ("-B", str(native.peer), *arguments),
                str(native.work),
                {**native.env, "LONG_VALUE": "x" * 9000},
                stdin=b"",
                inherit_control_environment=False,
            ),
        )
        assert result.outcome == "completed" and result.exit_code == 0
        observed = json.loads(result.stdout)
        assert observed["argv"] == list(arguments) and observed["argc"] == len(arguments)
        assert observed["long_value_length"] == 9000
        assert result.resolved_command == str(image)


@WINDOWS
@pytest.mark.parametrize("extension", [".exe", ".com"])
@pytest.mark.parametrize("failure", ["missing-image", "parent-file", "bad-cwd", "bad-image"])
async def test_windows_explicit_native_error_matches_actual_createprocess(
    native: WindowsFixture, failure: str, extension: str
) -> None:
    # Requirement: preflight cannot replace the real native error type/message/errno/filename.
    image = _copy_windows_image(native.root / "errors", "fixture" + extension)
    cwd = native.work
    match failure:
        case "missing-image":
            image = native.root / ("missing" + extension)
        case "parent-file":
            parent = native.root / "not-a-directory"
            parent.write_bytes(b"file")
            image = parent / ("missing" + extension)
        case "bad-cwd":
            cwd = native.root / "absent"
        case "bad-image":
            image = native.root / ("bad" + extension)
            image.write_bytes(b"not a PE")
    argv = (str(image), "-B", str(native.peer))
    with pytest.raises(OSError) as original:
        subprocess.run(argv, env=native.env, cwd=cwd, input=b"", capture_output=True, check=True)
    async with _native_controller(native.root) as controller:
        result = await controller.spawn_script(
            "error",
            CommandSpec(
                argv[0],
                argv[1:],
                str(cwd),
                native.env,
                stdin=b"",
                inherit_control_environment=False,
            ),
        )
        error = result.start_error
        assert error is not None and result.resolved_command == str(image)
        assert error.kind == (
            "file_not_found" if isinstance(original.value, FileNotFoundError) else "os_error"
        )
        assert (error.message, error.errno, error.filename) == (
            original.value.strerror,
            original.value.errno,
            original.value.filename,
        )


@WINDOWS
@pytest.mark.parametrize(
    "case",
    [
        "drive-relative",
        "rooted",
        "bad-path-quote",
        "quoted-semicolon",
        "unc-batch",
        "device-batch",
        "unc-cwd",
        "device-cwd",
        "association",
        "command-limit",
        "env-limit",
    ],
)
async def test_windows_context_refusals_have_no_payload(native: WindowsFixture, case: str) -> None:
    # Requirement: ambiguous paths, unsupported associations and cmd bounds refuse before dispatch.
    wrapper = _wrapper(native.work / "context.cmd")
    command, cwd, args, env = str(wrapper), str(native.work), (), dict(native.env)
    match case:
        case "drive-relative":
            command = "C:context.cmd"
        case "rooted":
            command = r"\context.cmd"
        case "bad-path-quote":
            command, env["PATH"] = "context", '"unbalanced'
        case "quoted-semicolon":
            command, env["PATH"] = "context", '"C:\\one;two"'
        case "unc-batch":
            command = r"\\server\share\context.cmd"
        case "device-batch":
            command = "\\\\?\\" + str(wrapper)
        case "unc-cwd":
            cwd = r"\\server\share\work"
        case "device-cwd":
            cwd = "\\\\?\\" + str(native.work)
        case "association":
            command = str(native.work / "context.ps1")
        case "command-limit":
            args = ("x" * 8191,)
        case "env-limit":
            env["LONG_VALUE"] = "x" * 8191
    before = set(native.receipts.iterdir())
    dispatch = Mock()
    async with _native_controller(native.root) as controller:
        result = await controller.spawn_script(
            "context",
            CommandSpec(
                command,
                args,
                cwd,
                env,
                stdin=b"",
                inherit_control_environment=False,
            ),
            on_dispatch=dispatch,
        )
        assert result.outcome == "start_failed" and result.start_error is not None
        assert result.start_error.errno is None and result.start_error.filename is None
        dispatch.assert_not_called()
        assert set(native.receipts.iterdir()) == before
        assert (await controller.await_release()).status == "released"
        assert all(
            proof.done and "COMMAND_STARTED" not in proof.events for proof in controller.proofs
        )


@WINDOWS
@pytest.mark.parametrize(
    "selection",
    [
        "declared",
        "systemroot",
        "declared-ignores-bad-root",
        "empty",
        "relative",
        "missing",
        "missing-systemroot",
        "empty-systemroot",
        "relative-systemroot",
        "unc-interpreter",
        "device-interpreter",
        "nul-comspec",
        "nul-systemroot",
    ],
)
async def test_windows_interpreter_uses_only_declared_context(
    native: WindowsFixture, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    # Requirement: a bad explicit ComSpec cannot fall back; only absence permits SystemRoot.
    wrapper = _wrapper(native.work / "interpreter.cmd")
    env = dict(native.env)
    monkeypatch.setenv("COMSPEC", str(native.root / "ambient-must-not-run.exe"))
    match selection:
        case "declared":
            env["ComSpec"] = env.pop("COMSPEC")
        case "systemroot":
            env.pop("COMSPEC")
        case "declared-ignores-bad-root":
            env["SYSTEMROOT"] = "relative-root"
        case "empty":
            env["COMSPEC"] = ""
        case "relative":
            env["COMSPEC"] = "cmd.exe"
        case "missing":
            env["COMSPEC"] = str(native.root / "missing-cmd.exe")
        case "missing-systemroot":
            env.pop("COMSPEC")
            env.pop("SYSTEMROOT")
        case "empty-systemroot":
            env.pop("COMSPEC")
            env["SYSTEMROOT"] = ""
        case "relative-systemroot":
            env.pop("COMSPEC")
            env["SYSTEMROOT"] = "relative-root"
        case "unc-interpreter":
            env["COMSPEC"] = r"\\server\share\cmd.exe"
        case "device-interpreter":
            env["COMSPEC"] = "\\\\?\\" + env["COMSPEC"]
        case "nul-comspec":
            env["COMSPEC"] += "\0"
        case "nul-systemroot":
            env.pop("COMSPEC")
            env["SYSTEMROOT"] += "\0"
    if selection == "declared-ignores-bad-root":
        # Interpreter selection is separate from the native loader's environment requirements.
        prepared, error = windows.prepare_windows_launch(
            ProcessLaunchSpec((str(wrapper),), env, str(native.work))
        )
        assert error is None
        assert windows.windows_create_process_arguments(prepared)[0] == env["COMSPEC"]
        return
    before = set(native.receipts.iterdir())
    async with _native_controller(native.root) as controller:
        result = await controller.spawn_script(
            "interpreter",
            CommandSpec(
                str(wrapper),
                (),
                str(native.work),
                env,
                stdin=b"",
                inherit_control_environment=False,
            ),
        )
        assert result.resolved_command == str(wrapper)
        if selection in ("declared", "systemroot"):
            assert result.outcome == "completed" and result.exit_code == 0
            assert json.loads(result.stdout)["cwd"] == str(native.work)
        else:
            assert result.start_error is not None
            assert result.outcome == (
                "command_not_found" if selection == "missing" else "start_failed"
            )
            assert set(native.receipts.iterdir()) == before


@WINDOWS
async def test_windows_post_start_batch_failure_is_completed_nonzero(
    native: WindowsFixture,
) -> None:
    # Requirement: starting cmd is not wrapper success; a real batch exit stays completed/nonzero.
    wrapper = _wrapper(native.work / "nonzero.bat")
    async with _native_controller(native.root) as controller:
        result = await controller.spawn_script(
            "nonzero",
            CommandSpec(
                str(wrapper),
                (),
                str(native.work),
                {**native.env, "WINDOWS_PEER_EXIT": "23"},
                stdin=b"",
                inherit_control_environment=False,
            ),
        )
        assert result.outcome == "completed" and result.exit_code == 23
        assert result.start_error is None and result.resolved_command == str(wrapper)
        assert json.loads(result.stdout)["stdin_hex"] == ""
        assert (await controller.await_release()).status == "released"
        (proof,) = controller.proofs
        assert "COMMAND_STARTED" in proof.events and "COMMAND_EXITED" in proof.events
        assert "PAYLOAD_START_FAILED" not in proof.events


@WINDOWS
@pytest.mark.parametrize("mode", ["raw", "descendant"])
async def test_windows_batch_preserves_raw_eof_bytes_and_descendant_custody(
    native: WindowsFixture, mode: str
) -> None:
    # Requirement: raw NUL/CRLF bytes survive facade EOF, including a redirected native descendant.
    wrapper = _wrapper(native.work / "bytes.cmd")
    env = {**native.env, "WINDOWS_PEER_MODE": mode}
    data = b"\0accepted\r\n\xff"
    async with _native_controller(native.root) as controller:
        launch = await controller.wrap_stdio(
            "bytes", ProcessLaunchSpec((str(wrapper),), env, str(native.work))
        )
        process = await asyncio.create_subprocess_exec(
            *launch.argv,
            env=dict(launch.env),
            cwd=launch.working_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if mode == "raw":
            assert process.stdout is not None
            assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
            controller.begin_teardown("bytes")
        out, err = await asyncio.wait_for(process.communicate(data), 10)
        assert process.returncode == 0
        assert out == (b"final:" + data if mode == "raw" else data)
        assert err == (b"err:\0\r\n" if mode == "raw" else b"descendant:\0\r\n")
        assert (await controller.await_release()).status == "released"
        (proof,) = controller.proofs
        assert proof.kernel_empty and proof.streams_drained and proof.facade_exited
        assert proof.custodian_reaped and proof.done and "FORCE_STOP" not in proof.events
        if mode == "descendant":
            (receipt,) = native.receipts.glob("*-leaf.json")
            observed = json.loads(receipt.read_text())
            assert observed["stdin_hex"] == data.hex() and observed["in_job"]


@WINDOWS
async def test_windows_managed_mcp_staged_bytes_and_targeted_sibling_close(
    native: WindowsFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: real MCP uses the declared wrapper and staged tree without closing a sibling.
    from conductor.execution.types import BundleRef, RunSpec
    from conductor.mcp.manager import MCPManager

    home = native.root / "retained-home"
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    store = native.root / "cas"
    source = store / "tree" / "main"
    source.mkdir(parents=True)
    (source / "shared.txt").write_text("original bundled bytes", encoding="utf-8")
    run = RunSpec(
        "windows-mcp",
        bundle=BundleRef("sha256:windows-fixture", str(store)),
        workspace_persistence="durable",
        local_workspace_root=str(home / "workspaces/windows-mcp/local"),
    )
    backend = local.LocalRunnerBackend()
    lease = await backend.prepare_run(run)
    binding = backend.local_workspace_binding(lease)
    assert binding is not None
    work = Path(binding.work_root) / "main"
    (work / "shared.txt").write_text("mutable staged bytes", encoding="utf-8")
    controller = backend._managed[lease.lease_id].controller
    first = MCPManager(managed_workspace=binding.runtime_context)
    sibling = MCPManager(managed_workspace=binding.runtime_context)
    wrapper = _wrapper(native.root / "declared-npm" / "npx.cmd")
    monkeypatch.setenv("PATH", str(native.root / "ambient"))
    monkeypatch.setenv("WINDOWS_PEER_AMBIENT", "not-granted")
    env = {key: value for key, value in native.env.items() if key != "PATH"}
    env.update({"Path": str(wrapper.parent), "WINDOWS_PEER_MODE": "mcp"})
    profile = native.root / "explicit-profile"
    profile.mkdir()
    env.pop("USERPROFILE")
    env["UserProfile"] = str(profile)
    try:
        for manager, name in ((first, "first"), (sibling, "sibling")):
            await manager.connect_server(
                name,
                "npx",
                ["-y", "@fixture/server", ""],
                env={**env, "WINDOWS_PEER_MARKER": name},
                cwd=str(work),
            )
        observed = json.loads(await first.call_tool("first__inspect", {}))
        assert observed["file"] == "mutable staged bytes"
        assert observed["cwd"] == str(work) and observed["argv"] == ["-y", "@fixture/server", ""]
        assert observed["path"] == str(wrapper.parent)
        assert observed["profile"] == str(profile)
        assert observed["ambient"] is None
        first_pid = observed["pid"]
        await first.close(strict=True)
        assert (native.receipts / f"{first_pid}-eof.json").is_file()
        other = json.loads(await sibling.call_tool("sibling__inspect", {}))
        assert other["file"] == "mutable staged bytes" and other["marker"] == "sibling"
        assert not (native.receipts / f"{other['pid']}-eof.json").exists()
    finally:
        await first.close(strict=True)
        await sibling.close(strict=True)
        assert (await controller.await_release()).status == "released"
        await backend.confirm_workspace_released(lease)
        await backend.finalize_run(lease, "succeeded", retain=True)
        await controller.close()
        await _join_callbacks(controller)
        controller._accepting.join(timeout=3)
        assert not controller._accepting.is_alive()
    assert (source / "shared.txt").read_text() == "original bundled bytes"
    assert (work / "shared.txt").read_text() == "mutable staged bytes"
    assert all(
        proof.done and proof.kernel_empty and proof.facade_exited for proof in controller.proofs
    )


@WINDOWS
@pytest.mark.parametrize("extension", [".exe", ".cmd", ".bat"])
async def test_windows_real_claude_sdk_probe_auth_and_main_final_contexts(
    native: WindowsFixture, monkeypatch: pytest.MonkeyPatch, extension: str
) -> None:
    # Requirement: SDK -v, auth and main preserve actual HELLO context and SDK-added arguments.
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

    from conductor.providers.claude_agent_sdk import _run_auth_status_subprocess

    probe_cwd = native.root / "probe"
    probe_cwd.mkdir()
    parent_env = {**native.env, "WINDOWS_PEER_MODE": "claude", "WINDOWS_PEER_MARKER": "probe"}
    for key in tuple(os.environ):
        monkeypatch.delenv(key)
    for key, value in parent_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(probe_cwd)
    main_env = {**parent_env, "WINDOWS_PEER_MARKER": "main"}
    target = (
        (str(native.image), "-B", str(native.peer))
        if extension == ".exe"
        else (str(_wrapper(native.work / ("claude" + extension))),)
    )
    async with _native_controller(native.root) as controller:
        launch = await controller.wrap_stdio(
            "claude", ProcessLaunchSpec(target, main_env, str(native.work))
        )
        out, err, code = await _run_auth_status_subprocess(
            Path(launch.argv[0]),
            {**main_env, "WINDOWS_PEER_MARKER": "auth"},
            str(native.work),
            [],
        )
        assert code == 0 and err == b"" and json.loads(out)["loggedIn"] is True
        client = ClaudeSDKClient(
            options=ClaudeAgentOptions(
                cli_path=launch.argv[0],
                cwd=str(native.work),
                env=main_env,
                tools=[],
                setting_sources=[],
                permission_mode="dontAsk",
            )
        )
        try:
            await client.connect()
            await client.query("hermetic input")
            messages = [message async for message in client.receive_response()]
            assert any(
                isinstance(message, ResultMessage) and message.result == "fixture response"
                for message in messages
            )
        finally:
            await client.disconnect()
        assert (await controller.await_release()).status == "released"
        version = [json.loads(path.read_text()) for path in native.receipts.glob("*-version.json")]
        auth = [json.loads(path.read_text()) for path in native.receipts.glob("*-auth.json")]
        main = [json.loads(path.read_text()) for path in native.receipts.glob("*-main.json")]
        assert len(version) == len(auth) == len(main) == 1
        assert version[0]["argv"] == ["-v"]
        assert version[0]["cwd"] == str(probe_cwd) and version[0]["marker"] == "probe"
        assert auth[0]["argv"] == ["--setting-sources=", "auth", "status", "--json"]
        assert auth[0]["cwd"] == str(native.work) and auth[0]["marker"] == "auth"
        argv = main[0]["argv"]
        assert argv[argv.index("--system-prompt") + 1] == ""
        assert argv[argv.index("--tools") + 1] == "" and "--setting-sources=" in argv
        assert argv[argv.index("--input-format") + 1] == "stream-json"
        assert main[0]["cwd"] == str(native.work) and main[0]["marker"] == "main"
        assert (native.receipts / f"{main[0]['pid']}-eof.json").is_file()
        assert len(controller.proofs) == 3 and all(proof.done for proof in controller.proofs)


@WINDOWS
@pytest.mark.parametrize("batch", [False, True])
async def test_windows_real_sdk_json_schema_is_preserved_or_refused_without_dropping_flags(
    native: WindowsFixture, monkeypatch: pytest.MonkeyPatch, batch: bool
) -> None:
    # Requirement: JSON argv works natively; batch refuses it without dropping or rewriting schema.
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        CLIConnectionError,
        ProcessError,
        ResultMessage,
    )

    for key, value in native.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
    monkeypatch.setenv("WINDOWS_PEER_MODE", "claude")
    monkeypatch.chdir(native.work)
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    target = (
        (str(_wrapper(native.work / "schema.cmd")),)
        if batch
        else (str(native.image), "-B", str(native.peer))
    )
    async with _native_controller(native.root) as controller:
        launch = await controller.wrap_stdio(
            "schema", ProcessLaunchSpec(target, dict(os.environ), str(native.work))
        )
        client = ClaudeSDKClient(
            options=ClaudeAgentOptions(
                cli_path=launch.argv[0],
                cwd=str(native.work),
                env=dict(os.environ),
                tools=[],
                setting_sources=[],
                permission_mode="dontAsk",
                output_format={"type": "json_schema", "schema": schema},
            )
        )
        try:
            if batch:
                with pytest.raises(
                    (CLIConnectionError, ProcessError, RuntimeError, ExceptionGroup)
                ):
                    await asyncio.wait_for(client.connect(), 10)
            else:
                await client.connect()
                await client.query("hermetic schema input")
                assert any(
                    isinstance(message, ResultMessage)
                    for message in [item async for item in client.receive_response()]
                )
        finally:
            await client.disconnect()
        assert (await controller.await_release()).status == "released"
        main = list(native.receipts.glob("*-main.json"))
        if batch:
            assert main == []
            failures = [
                record.error
                for record in controller._history.values()
                if record is not None and record.error is not None
            ]
            assert len(failures) == 1 and failures[0].errno is None
            assert failures[0].message.startswith("Unsupported literal batch character in argument")
            assert sum("COMMAND_STARTED" in proof.events for proof in controller.proofs) == 1
        else:
            assert len(main) == 1
            argv = json.loads(main[0].read_text())["argv"]
            assert argv[argv.index("--json-schema") + 1] == json.dumps(schema)


@WINDOWS
async def test_windows_real_copilot_sdk_preserves_bare_node_prefix_and_appended_flags(
    native: WindowsFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: the real SDK's appended flags/env survive a declared bare-node facade prefix.
    # The PE named node.exe is an explicit CLI test double; this does not exercise V8.
    from copilot import CopilotClient, RuntimeConnection

    node = _copy_windows_image(native.root / "node-bin", "node.exe")
    script = native.root / "fixture.js"
    shutil.copyfile(native.peer, script)
    monkeypatch.setenv("PATH", str(native.root / "parent-must-not-resolve-node"))
    env = {**native.env, "PATH": str(node.parent), "WINDOWS_PEER_MODE": "copilot"}
    async with _native_controller(native.root) as controller:
        launch = await controller.wrap_stdio(
            "copilot", ProcessLaunchSpec(("node", str(script)), env, str(native.work))
        )
        client = CopilotClient(
            connection=RuntimeConnection.for_stdio(path=launch.argv[0], args=("--literal", "")),
            working_directory=str(native.work),
            env=env,
            github_token="hermetic-token",
            use_logged_in_user=False,
            session_idle_timeout_seconds=17,
            base_directory=str(native.root / "copilot-home"),
        )
        try:
            await client.start()
            response = await client.ping("hermetic-ping")
            assert response.message == "hermetic-ping"
        finally:
            await client.stop()
        assert (await controller.await_release()).status == "released"
        (path,) = native.receipts.glob("*-copilot.json")
        observed = json.loads(path.read_text())
        assert Path(observed["executable"]) == node and observed["script"] == str(script)
        assert observed["argv"] == [
            "--literal",
            "",
            "--headless",
            "--no-auto-update",
            "--log-level",
            "info",
            "--auth-token-env",
            "COPILOT_SDK_AUTH_TOKEN",
            "--no-auto-login",
            "--session-idle-timeout",
            "17",
            "--stdio",
        ]
        assert observed["sdk_token"] == "hermetic-token" and observed["cwd"] == str(native.work)
        assert (native.receipts / f"{observed['pid']}-eof.json").is_file()


@WINDOWS
def test_windows_batch_read_failure_preserves_actual_os_error(native: WindowsFixture) -> None:
    # Requirement: a sharing/access failure cannot become lookup-missing or another candidate.
    import _winapi
    from ctypes import wintypes

    wrapper = _wrapper(native.work / "locked.cmd")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(wrapper), 0x80000000, 0, None, 3, 0, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        with pytest.raises(OSError) as original:
            wrapper.read_bytes()
        prepared, error = windows.prepare_windows_launch(
            ProcessLaunchSpec((str(wrapper),), native.env, str(native.work))
        )
        assert prepared.argv == (str(wrapper),) and error is not None
        assert error.kind == "os_error"
        assert (error.message, error.errno, error.filename) == (
            original.value.strerror,
            original.value.errno,
            original.value.filename,
        )
    finally:
        _winapi.CloseHandle(handle)


@WINDOWS
@pytest.mark.parametrize("batch", [False, True])
def test_windows_real_suspended_job_assignment_and_handle_allowlist(
    native: WindowsFixture, monkeypatch: pytest.MonkeyPatch, batch: bool
) -> None:
    # Requirement: suspended creation precedes non-breakaway assignment/resume.
    # Only the three stdio handles may be inherited by the actual payload.
    import _winapi
    import msvcrt
    from ctypes import wintypes

    from conductor.execution import local_scope_launcher as launcher

    tree = launcher._WindowsTree()
    kernel = launcher._kernel
    kernel.SuspendThread.argtypes = [wintypes.HANDLE]
    kernel.SuspendThread.restype = wintypes.DWORD
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    create, assign, resume = (
        _winapi.CreateProcess,
        kernel.AssignProcessToJobObject,
        kernel.ResumeThread,
    )
    order: list[str] = []
    owned_threads: list[threading.Thread] = []
    env = dict(native.env)
    if batch:
        interpreter = native.root / "declared-interpreter" / "cmd.exe"
        interpreter.parent.mkdir()
        shutil.copyfile(native.env["COMSPEC"], interpreter)
        env["COMSPEC"] = str(interpreter)
    wrapper = _wrapper(native.work / "kernel.cmd")
    argv = (
        [str(wrapper), "kernel"] if batch else [str(native.image), "-B", str(native.peer), "kernel"]
    )

    def new_thread(*, target: Callable[[], None], daemon: bool) -> threading.Thread:
        thread = threading.Thread(target=target, daemon=daemon)
        owned_threads.append(thread)
        return thread

    def observe_create(
        application, command, process_attrs, thread_attrs, inherit, flags, child_env, cwd, si
    ):
        assert application == (env["COMSPEC"] if batch else None)
        assert child_env == env
        assert flags & 4 and flags & 0x400 and inherit
        assert si.dwFlags & subprocess.STARTF_USESTDHANDLES
        assert si.lpAttributeList["handle_list"] == [si.hStdInput, si.hStdOutput, si.hStdError]
        if batch:
            assert command.startswith(f'"{env["COMSPEC"]}" /d /e:on /v:off /s /c "')
        result = create(
            application, command, process_attrs, thread_attrs, inherit, flags, child_env, cwd, si
        )
        process, thread, _pid, _tid = result
        try:
            # Increment and restore suspension without running an unassigned thread.
            assert kernel.SuspendThread(thread) == 1
            assert resume(thread) == 2
            buffer, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
            assert kernel.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size))
            assert Path(buffer.value) == Path(env["COMSPEC"] if batch else native.image)
        except (AssertionError, OSError):
            _winapi.TerminateProcess(process, 125)
            _winapi.WaitForSingleObject(process, _winapi.INFINITE)
            _winapi.CloseHandle(thread)
            _winapi.CloseHandle(process)
            raise
        order.append("suspended")
        return result

    def observe_assign(job: int, process: int) -> int:
        result = assign(job, process)
        assert result
        member = wintypes.BOOL()
        assert kernel.IsProcessInJob(process, job, ctypes.byref(member)) and member.value
        limits = launcher._ExtendedLimit()
        assert kernel.QueryInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits), None
        )
        assert limits.basic.flags & 0x2000 and not limits.basic.flags & (0x800 | 0x1000)
        order.append("assigned")
        return result

    def observe_resume(thread: int) -> int:
        assert order == ["suspended", "assigned"]
        result = resume(thread)
        assert result == 1
        order.append("resumed")
        return result

    extra = native.root / "extra-handle.txt"
    with extra.open("wb") as stream, monkeypatch.context() as patched:
        extra_handle = msvcrt.get_osfhandle(stream.fileno())
        os.set_handle_inheritable(extra_handle, True)
        env["WINDOWS_PEER_EXTRA_HANDLE"] = str(extra_handle)
        patched.setattr(_winapi, "CreateProcess", observe_create)
        patched.setattr(kernel, "AssignProcessToJobObject", Mock(side_effect=observe_assign))
        patched.setattr(kernel, "ResumeThread", Mock(side_effect=observe_resume))
        patched.setattr(launcher, "threading", SimpleNamespace(Thread=new_thread))
        try:
            tree.start(argv, env, str(native.work))
            assert tree.stdin is not None and tree.stdout is not None and tree.stderr is not None
            tree.stdin.close()
            assert tree.handle is not None
            assert _winapi.WaitForSingleObject(tree.handle, 10000) == _winapi.WAIT_OBJECT_0
            with os.fdopen(os.dup(tree.stdout.fileno()), "rb") as output:
                observed = json.loads(output.read())
            with os.fdopen(os.dup(tree.stderr.fileno()), "rb") as errors:
                assert errors.read() == b""
            assert (
                observed["extra_file"] is None
                or Path(observed["extra_file"].removeprefix("\\\\?\\")) != extra
            )
            assert observed["argv"] == ["kernel"] and observed["in_job"]
            tree.reap()
            assert tree.empty and order == ["suspended", "assigned", "resumed"]
        finally:
            tree.reap()
            if not tree.empty:
                tree.force()
            for thread in owned_threads:
                thread.join(timeout=3)
                assert not thread.is_alive()
            tree.close()


@WINDOWS
def test_windows_actual_assignment_failure_terminates_unexecuted_payload(
    native: WindowsFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: kernel assignment failure stays infrastructure failure and cannot run payload.
    import _winapi

    from conductor.execution import local_scope_launcher as launcher

    tree = launcher._WindowsTree()
    assign = launcher._kernel.AssignProcessToJobObject
    create, close = _winapi.CreateProcess, _winapi.CloseHandle
    created: list[int] = []
    closed: list[int] = []
    before = set(native.receipts.iterdir())

    def observe_create(*args):
        result = create(*args)
        created.append(result[0])
        return result

    def observe_close(handle: int) -> None:
        if handle in created:
            assert _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_OBJECT_0
            assert _winapi.GetExitCodeProcess(handle) == 125
            closed.append(handle)
        close(handle)

    with monkeypatch.context() as patched:
        patched.setattr(_winapi, "CreateProcess", observe_create)
        patched.setattr(_winapi, "CloseHandle", observe_close)
        # Invoke the actual API with an invalid job, rather than faking a negative return.
        patched.setattr(
            launcher._kernel,
            "AssignProcessToJobObject",
            Mock(side_effect=lambda _job, process: assign(0, process)),
        )
        try:
            with pytest.raises(launcher.CustodyError, match="assign suspended payload"):
                tree.start(
                    [str(native.image), "-B", str(native.peer)], native.env, str(native.work)
                )
            tree.reap()
            assert tree.empty and len(created) == 1 and closed == created
            assert set(native.receipts.iterdir()) == before
        finally:
            tree.close()


@WINDOWS
async def test_windows_child_revalidates_known_policy_without_native_error_fabrication(
    native: WindowsFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: bypassing the parent check cannot let the child execute unsupported batch argv.
    wrapper = _wrapper(native.work / "child-policy.cmd")
    before = set(native.receipts.iterdir())
    monkeypatch.setattr(process_core, "prepare_windows_launch", lambda launch: (launch, None))
    async with _native_controller(native.root) as controller:
        result = await controller.spawn_script(
            "child-policy",
            CommandSpec(
                str(wrapper),
                ('"unsupported"',),
                str(native.work),
                native.env,
                stdin=b"",
                inherit_control_environment=False,
            ),
        )
        assert result.outcome == "start_failed" and result.start_error is not None
        assert result.start_error.errno is None and result.start_error.filename is None
        assert result.start_error.message == "Unsupported literal batch character in argument 1"
        assert set(native.receipts.iterdir()) == before
        assert (await controller.await_release()).status == "released"
        (proof,) = controller.proofs
        assert "PAYLOAD_START_FAILED" in proof.events and "COMMAND_STARTED" not in proof.events
        assert proof.done and proof.kernel_empty and proof.streams_drained


@WINDOWS
async def test_windows_native_scope_keeps_original_deadline_and_sticky_owner_uncertainty(
    native: WindowsFixture,
) -> None:
    # Requirement: a real Windows payload cannot reset an earlier deadline or upgrade a late owner.
    from tests.test_mcp.test_manager_managed_workspace import ControlledClock

    clock = ControlledClock()
    controller = LocalProcessScopeController(str(native.root), "deadline", _scheduler=clock)
    owner: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    controller.owners.register(None, owner)
    try:
        wrapper = _wrapper(native.work / "deadline.cmd")
        result = await controller.spawn_script(
            "deadline",
            CommandSpec(
                str(wrapper),
                (),
                str(native.work),
                native.env,
                stdin=b"",
                inherit_control_environment=False,
            ),
        )
        assert result.exit_code == 0 and json.loads(result.stdout)["in_job"]
        deadline = clock.now() + 3
        controller.begin_teardown(deadline=deadline)
        controller.begin_teardown(deadline=deadline + 100)
        assert controller.owners.deadline() == deadline
        clock.advance(deadline)
        assert (await controller.await_release()).status == "unconfirmed"
        owner.set_result(None)
        await _join_callbacks(controller)
        assert (await controller.await_release()).status == "unconfirmed"
        assert all(proof.kernel_empty and proof.done for proof in controller.proofs)
    finally:
        if not owner.done():
            owner.set_result(None)
        await controller.close()
        await _join_callbacks(controller)
        controller._accepting.join(timeout=3)
        assert not controller._accepting.is_alive()


@WINDOWS
async def test_windows_actual_installed_helper_executes_current_windows_module(
    native: WindowsFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: copied entrypoints execute the paired module bytes in a real child.
    audit = native.root / "audit"
    audit.mkdir()
    (audit / "sitecustomize.py").write_text(
        "from windows_launch_fixture import install_audit\ninstall_audit()\n", encoding="utf-8"
    )
    monkeypatch.setenv("WINDOWS_PEER_AUDIT", str(audit))
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            (
                str(audit),
                str(Path(__file__).parent),
                native.env["PYTHONPATH"],
            )
        ),
    )
    helper = Path(sysconfig.get_path("scripts")) / "conductor-local-scope.exe"
    expected_stub = hashlib.sha256(helper.read_bytes()).hexdigest()
    expected_module = hashlib.sha256(Path(windows.__file__).read_bytes()).hexdigest()
    async with _native_controller(native.root) as controller:
        result = await controller.spawn_script(
            "origin",
            CommandSpec(
                str(native.image),
                ("-B", str(native.peer)),
                str(native.work),
                native.env,
                stdin=b"",
                inherit_control_environment=False,
            ),
        )
        assert result.exit_code == 0
        assert (await controller.await_release()).status == "released"
    records = [json.loads(path.read_text()) for path in audit.glob("*-local_windows_launch.json")]
    assert len(records) == 1
    record = records[0]
    assert record["pid"] != os.getpid() and ".scopes" in Path(record["entrypoint"]).parts
    assert record["entrypoint_hash"] == expected_stub
    assert record["sha256"] == expected_module and record["source_code_equal"]


@pytest.mark.skipif(sys.platform != "linux", reason="Real Linux infrastructure wiring control")
async def test_unexpected_preparation_exception_is_owned_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: an unexpected preparation bug reaches the owner instead of stranding its future.
    controller = LocalProcessScopeController(str(tmp_path), "preparation-failure")
    monkeypatch.setattr(
        process_core, "prepare_windows_launch", Mock(side_effect=RuntimeError("fixture bug"))
    )
    try:
        with monkeypatch.context() as platform:
            platform.setattr(process_core, "sys", SimpleNamespace(platform="win32"))
            result = await asyncio.wait_for(
                controller.spawn_script(
                    "failure",
                    CommandSpec(
                        r"C:\fixture.exe", working_dir=r"C:\work", inherit_control_environment=False
                    ),
                ),
                10,
            )
        assert result.outcome == "start_failed" and result.start_error is not None
        assert result.start_error.message == "Managed process infrastructure failure"
        assert (await controller.await_release()).status == "unconfirmed"
        for scope in tuple(controller._scopes.values()):
            assert scope.process is not None
            await asyncio.wait_for(scope.process.wait(), 10)
        await _join_callbacks(controller)
        assert all(not proof.done for proof in controller.proofs)
    finally:
        await controller.close()
        await _join_callbacks(controller)
        controller._accepting.join(timeout=3)
        assert not controller._accepting.is_alive()


@pytest.mark.skipif(sys.platform != "linux", reason="Real Linux refusal transport control")
async def test_refusal_control_loss_never_becomes_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: losing the refusal channel preserves policy data and uncertain cleanup.
    controller = LocalProcessScopeController(str(tmp_path), "refusal-control")
    original_send = controller._send

    def close_rejection_channel(scope, channel, frame):
        if frame["event"] == "REJECTED":
            controller._disconnect(channel)
        return original_send(scope, channel, frame)

    monkeypatch.setattr(controller, "_send", close_rejection_channel)
    try:
        with monkeypatch.context() as platform:
            platform.setattr(process_core, "sys", SimpleNamespace(platform="win32"))
            result = await asyncio.wait_for(
                controller.spawn_script(
                    "refused",
                    CommandSpec(
                        r"C:\refused.cmd",
                        ('"unsupported"',),
                        r"C:\work",
                        {"COMSPEC": r"C:\Windows\System32\cmd.exe"},
                        inherit_control_environment=False,
                    ),
                ),
                10,
            )
        assert result.outcome == "start_failed" and result.start_error is not None
        assert result.start_error.message == "Unsupported literal batch character in argument 1"
        assert result.start_error.errno is None
        assert (await controller.await_release()).status == "unconfirmed"
        for scope in tuple(controller._scopes.values()):
            assert scope.process is not None
            await asyncio.wait_for(scope.process.wait(), 10)
        await _join_callbacks(controller)
        assert all(not proof.done for proof in controller.proofs)
    finally:
        await controller.close()
        await _join_callbacks(controller)
        controller._accepting.join(timeout=3)
        assert not controller._accepting.is_alive()


@WINDOWS
@pytest.mark.parametrize("case", ["equal-aliases", "conflicting-aliases", "reserved-alias"])
async def test_windows_script_environment_identity_is_enforced_before_payload(
    native: WindowsFixture, case: str
) -> None:
    # Requirement: final script merge collapses equal aliases and refuses ambiguous/reserved names.
    wrapper = _wrapper(native.work / "environment.cmd")
    env = dict(native.env)
    if case == "reserved-alias":
        env["conductor_local_scope_token"] = "not-a-grant"
    else:
        env["Path"] = env["PATH"] if case == "equal-aliases" else "conflicting"
    before = set(native.receipts.iterdir())
    dispatch = Mock()
    async with _native_controller(native.root) as controller:
        result = await controller.spawn_script(
            "environment",
            CommandSpec(
                str(wrapper),
                (),
                str(native.work),
                env,
                stdin=b"",
                inherit_control_environment=False,
            ),
            on_dispatch=dispatch,
        )
        if case == "equal-aliases":
            assert result.exit_code == 0
            assert json.loads(result.stdout)["path"] == native.env["PATH"]
            dispatch.assert_called_once()
        else:
            assert result.outcome == "start_failed" and result.start_error is not None
            assert result.start_error.errno is None and result.start_error.filename is None
            dispatch.assert_not_called()
            assert set(native.receipts.iterdir()) == before
        assert (await controller.await_release()).status == "released"
        assert all(proof.done and proof.kernel_empty for proof in controller.proofs)


def test_native_unc_selector_is_lexically_delegated_without_network_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: ordinary native UNC images retain CreateProcess authority without a pre-stat.
    stat = Mock(side_effect=AssertionError("No network or pre-stat for an explicit native image"))
    monkeypatch.setattr(windows, "os", SimpleNamespace(stat=stat))
    launch = ProcessLaunchSpec((r"\\server\share\image.exe", "literal"), {}, r"C:\work")
    prepared, error = windows.prepare_windows_launch(launch)
    assert error is None and prepared.argv == launch.argv
    assert windows.windows_create_process_arguments(prepared) == (
        None,
        r"\\server\share\image.exe literal",
    )
    stat.assert_not_called()


@pytest.mark.parametrize("unexpected", [False, True])
def test_search_does_not_hide_os_or_programmer_failures(
    monkeypatch: pytest.MonkeyPatch, unexpected: bool
) -> None:
    # Requirement: access failure cannot select another image; programmer errors remain exceptions.
    failure = (
        RuntimeError("fixture bug")
        if unexpected
        else PermissionError(errno.EACCES, "fixture access error", r"C:\declared\image.EXE")
    )
    stat = Mock(side_effect=failure)
    monkeypatch.setattr(windows, "os", SimpleNamespace(stat=stat))
    launch = ProcessLaunchSpec(
        ("image",), {"PATH": r"C:\declared;C:\fallback", "PATHEXT": ".EXE"}, r"C:\work"
    )
    if unexpected:
        with pytest.raises(RuntimeError) as caught:
            windows.prepare_windows_launch(launch)
        assert caught.value is failure
    else:
        prepared, error = windows.prepare_windows_launch(launch)
        assert prepared.argv == ("image",) and error is not None
        assert (error.kind, error.message, error.errno, error.filename) == (
            "os_error",
            "fixture access error",
            errno.EACCES,
            r"C:\declared\image.EXE",
        )
    stat.assert_called_once_with(r"C:\declared\image.EXE")


@WINDOWS
@pytest.mark.parametrize("kind", ["command", "environment"])
@pytest.mark.parametrize("extra", [0, 1])
async def test_windows_actual_batch_utf16_bounds(
    native: WindowsFixture, kind: str, extra: int
) -> None:
    # Requirement: the conservative UTF-16 boundary succeeds exactly and refuses the next unit.
    wrapper = native.work / "bounded.cmd"
    marker = native.work / "bounded.txt"
    env = {**native.env, "WINDOWS_PEER_LIMIT_MARKER": str(marker)}
    arguments: tuple[str, ...] = ()
    if kind == "command":
        wrapper.write_bytes(b'@echo off\r\n> "%WINDOWS_PEER_LIMIT_MARKER%" echo bounded\r\n')
        prefix = f'"{env["COMSPEC"]}" /d /e:on /v:off /s /c ""{wrapper}" "'
        available = 8191 - 1 - len((prefix + '""').encode("utf-16-le")) // 2 + extra
        arguments = ("\U0001f642" * (available // 2) + "x" * (available % 2),)
    else:
        _wrapper(wrapper)
        env["LONG_VALUE"] = "x" * (8191 - len("LONG_VALUE=") - 1 + extra)
    dispatch = Mock()
    async with _native_controller(native.root) as controller:
        result = await controller.spawn_script(
            "bounds",
            CommandSpec(
                str(wrapper),
                arguments,
                str(native.work),
                env,
                stdin=b"",
                inherit_control_environment=False,
            ),
            on_dispatch=dispatch,
        )
        if extra:
            assert result.outcome == "start_failed" and result.start_error is not None
            assert result.start_error.errno is None
            dispatch.assert_not_called()
            assert not marker.exists()
        else:
            assert result.exit_code == 0
            dispatch.assert_called_once()
            if kind == "command":
                assert marker.read_bytes() == b"bounded\r\n"
            else:
                assert json.loads(result.stdout)["long_value_length"] == len(env["LONG_VALUE"])


@WINDOWS
async def test_windows_batch_interpreter_loader_failure_keeps_actual_start_error(
    native: WindowsFixture,
) -> None:
    # Requirement: an explicitly selected regular interpreter can still fail in real CreateProcess.
    wrapper = _wrapper(native.work / "interpreter-error.cmd")
    interpreter = native.root / "invalid-cmd.exe"
    interpreter.write_bytes(b"not a PE")
    env = {**native.env, "COMSPEC": str(interpreter)}
    with pytest.raises(OSError) as original:
        subprocess.run(
            [str(interpreter), "/d", "/e:on", "/v:off", "/s", "/c", str(wrapper)],
            executable=str(interpreter),
            env=env,
            cwd=native.work,
            input=b"",
            capture_output=True,
        )
    async with _native_controller(native.root) as controller:
        result = await controller.spawn_script(
            "interpreter-error",
            CommandSpec(
                str(wrapper),
                (),
                str(native.work),
                env,
                stdin=b"",
                inherit_control_environment=False,
            ),
        )
        assert result.outcome == "start_failed" and result.resolved_command == str(wrapper)
        assert result.start_error is not None
        assert (
            result.start_error.message,
            result.start_error.errno,
            result.start_error.filename,
        ) == (original.value.strerror, original.value.errno, original.value.filename)
        assert (await controller.await_release()).status == "released"
        (proof,) = controller.proofs
        assert "PAYLOAD_START_FAILED" in proof.events and "COMMAND_STARTED" not in proof.events
