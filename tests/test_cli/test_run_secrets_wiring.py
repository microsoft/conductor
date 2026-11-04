"""CLI wiring for the run-scoped secret machinery (``run`` and ``resume``).

Covers the CLI-owned lifecycle: environment resolution and secret preflight
happen before the inputs panel and before MCP server construction; the engine
receives the CLI's cache/redactor pair by injection; the redactor is attached
to the emitter and set as the contextvar before any sink can emit; and both
are cleared (with the contextvar restored via token) in the outermost finally.
"""

from __future__ import annotations

import errno
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor import redaction
from conductor.cli.run import _build_mcp_servers
from conductor.config.schema import (
    MCPServerDef,
    RouteDef,
    RuntimeConfig,
    SetStepDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.checkpoint import CheckpointManager
from conductor.engine.secrets import SecretValueCache
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import ConfigurationError, WorkflowTerminated
from conductor.redaction import REDACTED_MARKER, RunRedactor

_CLI_TOKEN_VAR = "CONDUCTOR_TEST_TASK7_CLI_TOKEN"

_SECRET_WORKFLOW = """\
workflow:
  name: cli-secrets
  entry_point: run
agents:
  - name: run
    type: script
    command: echo
    execution:
      secrets:
        - ref: token
          scope: script
          delivery:
            env: DELIVERED_TOKEN
    routes:
      - to: $end
output:
  result: "{{ run.output.stdout }}"
"""

_PLAIN_WORKFLOW = """\
workflow:
  name: cli-plain
  entry_point: mark
agents:
  - name: mark
    type: set
    value: "'ok'"
    routes:
      - to: $end
output:
  result: "{{ mark.output }}"
"""

_ENV_YAML = """\
default: shell
profiles:
  shell:
    backend: local
secrets:
  token:
    source:
      env: CONDUCTOR_TEST_TASK7_CLI_TOKEN
"""


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep run/terminal records out of the developer's real ``~/.conductor``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "conductor-home"))


def _write_workflow(tmp_path: Path, body: str = _SECRET_WORKFLOW) -> Path:
    wf = tmp_path / "workflow.yaml"
    wf.write_text(body, encoding="utf-8")
    return wf


def _write_environment(tmp_path: Path, name: str = "demo") -> Path:
    env_dir = tmp_path / ".conductor" / "environments"
    env_dir.mkdir(parents=True)
    env_path = env_dir / f"{name}.yaml"
    env_path.write_text(_ENV_YAML, encoding="utf-8")
    return env_path


def _write_checkpoint(
    tmp_path: Path, workflow_path: Path, *, event_log_path: Path | None = None
) -> Path:
    """A minimal resumable checkpoint (mirrors test_environment_flag)."""
    checkpoint = {
        "version": 1,
        "workflow_path": str(workflow_path.resolve()),
        "workflow_hash": CheckpointManager.compute_workflow_hash(workflow_path),
        "created_at": "2026-02-24T15:30:00+00:00",
        "failure": {
            "error_type": "ProviderError",
            "message": "Network error",
            "agent": "run",
            "iteration": 1,
        },
        "inputs": {},
        "current_agent": "run",
        "context": {
            "workflow_inputs": {},
            "agent_outputs": {},
            "current_iteration": 0,
            "execution_history": [],
        },
        "limits": {
            "current_iteration": 0,
            "max_iterations": 10,
            "execution_history": [],
        },
        "copilot_session_ids": {},
        "run_id": "",
        "event_log_path": str(event_log_path) if event_log_path is not None else "",
    }
    cp_path = tmp_path / f"{workflow_path.stem}-20260224-153000.json"
    cp_path.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
    return cp_path


def _mock_registry_and_engine(
    mock_registry_cls: MagicMock,
    mock_engine_cls: MagicMock,
    method: str,
    engine: MagicMock | None = None,
) -> MagicMock:
    """Wire the standard mocks: async ProviderRegistry + fixed-result engine."""
    mock_registry = AsyncMock()
    mock_registry_cls.return_value = mock_registry
    mock_registry.__aenter__ = AsyncMock(return_value=mock_registry)
    mock_registry.__aexit__ = AsyncMock(return_value=False)

    mock_engine = engine or MagicMock()
    setattr(mock_engine, method, AsyncMock(return_value={"result": "ok"}))
    mock_engine.config.workflow.cost.show_summary = False
    mock_engine_cls.return_value = mock_engine
    return mock_engine


@pytest.mark.asyncio
async def test_run_injects_cli_owned_pair_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: run resolves the environment, prepares the redactor/cache
    # pair, injects both into the engine constructor, and clears them (plus the
    # contextvar) after the run's final sinks drain.
    monkeypatch.setenv(_CLI_TOKEN_VAR, "cli-secret-value-1")
    wf_path = _write_workflow(tmp_path)
    _write_environment(tmp_path)

    from conductor.cli.run import run_workflow_async

    with (
        patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
        patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
    ):
        _mock_registry_and_engine(mock_registry_cls, mock_engine_cls, "run")
        await run_workflow_async(wf_path, {}, environment="demo")

    kwargs = mock_engine_cls.call_args.kwargs
    cache = kwargs["secret_cache"]
    redactor = kwargs["redactor"]
    assert isinstance(cache, SecretValueCache)
    assert isinstance(redactor, RunRedactor)
    # CLI-owned cleanup ran: the pair is cleared and the contextvar restored.
    assert not redactor.active
    assert redaction.current() is None
    with pytest.raises(ConfigurationError, match="has not been resolved"):
        cache.secret_for("token")


@pytest.mark.asyncio
async def test_run_prepares_secrets_before_inputs_print_and_scrubs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: the redactor exists BEFORE the inputs panel prints, and the
    # printed panel content is scrubbed of declared secret values.
    secret = "input-secret-value-9"
    monkeypatch.setenv(_CLI_TOKEN_VAR, secret)
    wf_path = _write_workflow(tmp_path)
    _write_environment(tmp_path)

    order: list[str] = []
    printed: list[str] = []

    real_set_redactor = WorkflowEventEmitter.set_redactor

    def spy_set_redactor(self: WorkflowEventEmitter, redactor: RunRedactor | None) -> None:
        order.append("set_redactor")
        return real_set_redactor(self, redactor)

    monkeypatch.setattr(WorkflowEventEmitter, "set_redactor", spy_set_redactor)

    from conductor.cli import run as run_module

    real_section = run_module.verbose_log_section

    def spy_section(title: str, content: str) -> None:
        order.append(f"section:{title}")
        printed.append(content)
        return real_section(title, content)

    monkeypatch.setattr(run_module, "verbose_log_section", spy_section)

    from conductor.cli.run import run_workflow_async

    with (
        patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
        patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
    ):
        _mock_registry_and_engine(mock_registry_cls, mock_engine_cls, "run")
        await run_workflow_async(wf_path, {"key": secret}, environment="demo")

    assert order.index("set_redactor") < order.index("section:Workflow Inputs")
    inputs_panel = printed[order.index("section:Workflow Inputs") - 1]
    assert secret not in inputs_panel
    assert REDACTED_MARKER in inputs_panel


@pytest.mark.asyncio
async def test_inputs_panel_scrubs_before_serialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement (review b4): secrets containing characters JSON escaping
    # rewrites (quotes, backslashes, newlines) must still be scrubbed from
    # the inputs panel — the scrub happens on the inputs mapping BEFORE
    # json.dumps, not on the serialized string. The caller's inputs dict is
    # never mutated.
    secret = 'quote"back\\slash\nnewline-secret-77'
    monkeypatch.setenv(_CLI_TOKEN_VAR, secret)
    wf_path = _write_workflow(tmp_path)
    _write_environment(tmp_path)

    printed: list[str] = []

    from conductor.cli import run as run_module

    real_section = run_module.verbose_log_section

    def spy_section(title: str, content: str) -> None:
        printed.append(content)
        return real_section(title, content)

    monkeypatch.setattr(run_module, "verbose_log_section", spy_section)

    inputs = {"key": secret}
    from conductor.cli.run import run_workflow_async

    with (
        patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
        patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
    ):
        _mock_registry_and_engine(mock_registry_cls, mock_engine_cls, "run")
        await run_workflow_async(wf_path, inputs, environment="demo")

    assert printed == [json.dumps({"key": REDACTED_MARKER}, indent=2, ensure_ascii=False)]
    assert inputs == {"key": secret}


@pytest.mark.asyncio
class TestEscapingExceptionSink:
    """Any exception escaping run/resume has its CLI-rendered fields scrubbed."""

    @pytest.mark.parametrize("resume", [False, True])
    async def test_exception_group_messages_and_children_are_scrubbed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume: bool
    ) -> None:
        # Requirement: immutable group messages and nested diagnostics stay scrubbed after cleanup.
        import traceback

        from conductor.cli.run import resume_workflow_async, run_workflow_async

        secret = "nested-exception-group-secret"
        monkeypatch.setenv(_CLI_TOKEN_VAR, secret)
        path = _write_workflow(tmp_path)
        _write_environment(tmp_path)
        leaf = ConfigurationError(secret, suggestion=secret, file_path=secret)
        nested = ExceptionGroup(secret, [RuntimeError(secret)])
        failure = ExceptionGroup(secret, [leaf, nested])
        with (
            patch("conductor.cli.run.ProviderRegistry") as registry_cls,
            patch("conductor.cli.run.WorkflowEngine") as engine_cls,
        ):
            engine = MagicMock()
            engine.run = AsyncMock(side_effect=failure)
            engine.resume = AsyncMock(side_effect=failure)
            engine.config.workflow.cost.show_summary = False
            _wire_registry_with_engine(registry_cls, engine_cls, engine)
            with pytest.raises(ExceptionGroup) as raised:
                if resume:
                    await resume_workflow_async(
                        workflow_path=path,
                        checkpoint_path=_write_checkpoint(tmp_path, path),
                        environment="demo",
                    )
                else:
                    await run_workflow_async(path, {}, environment="demo")
        assert raised.value.exceptions[0] is leaf
        assert secret not in "".join(traceback.format_exception(raised.value))
        assert REDACTED_MARKER in str(raised.value)

    @pytest.mark.asyncio
    async def test_run_configuration_error_structured_fields_are_scrubbed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Requirement (review fix B): run scrubs every ConfigurationError field
        # consumed by the real CLI renderer before clearing its redactor.
        secret = "escaped-exception-secret-1"
        monkeypatch.setenv(_CLI_TOKEN_VAR, secret)
        wf_path = _write_workflow(tmp_path)
        _write_environment(tmp_path)

        failure = ConfigurationError(
            f"configuration contains {secret}",
            suggestion=f"rotate the credential {secret}",
            file_path=f"/private/{secret}/workflow.yaml",
            line_number=42,
            field_path=f"workflow.{secret}.token",
        )

        from conductor.cli.run import run_workflow_async

        with (
            patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
            patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
        ):
            engine = MagicMock()
            engine.run = AsyncMock(side_effect=failure)
            engine.config.workflow.cost.show_summary = False
            _wire_registry_with_engine(mock_registry_cls, mock_engine_cls, engine)
            with pytest.raises(ConfigurationError) as exc_info:
                await run_workflow_async(wf_path, {}, environment="demo")

        raised = exc_info.value
        assert raised is failure
        assert raised.line_number == 42
        for value in (raised.args[0], raised.suggestion, raised.file_path, raised.field_path):
            assert value is not None
            assert secret not in value
            assert REDACTED_MARKER in value

        # The actual CLI error renderer stays clean after cleanup cleared it.
        from conductor.cli.app import print_error

        print_error(raised)
        rendered = capsys.readouterr().err
        assert secret not in rendered
        assert rendered.count(REDACTED_MARKER) == 4

    @pytest.mark.asyncio
    async def test_resume_configuration_error_structured_fields_are_scrubbed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Requirement (review fix B): resume provides the same complete
        # ConfigurationError rendering boundary as a fresh run.
        secret = "resume-exception-secret-2"
        monkeypatch.setenv(_CLI_TOKEN_VAR, secret)
        wf_path = _write_workflow(tmp_path)
        _write_environment(tmp_path)
        cp_path = _write_checkpoint(tmp_path, wf_path)

        failure = ConfigurationError(
            f"resumed configuration contains {secret}",
            suggestion=f"replace {secret}",
            file_path=f"/private/{secret}/resume.yaml",
            line_number=7,
            field_path=f"agents.{secret}.model",
        )

        from conductor.cli.run import resume_workflow_async

        with (
            patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
            patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
            patch("conductor.cli.run._write_terminal_record_for_current_process"),
        ):
            engine = MagicMock()
            engine.resume = AsyncMock(side_effect=failure)
            engine.config.workflow.cost.show_summary = False
            _wire_registry_with_engine(mock_registry_cls, mock_engine_cls, engine)
            with pytest.raises(ConfigurationError) as exc_info:
                await resume_workflow_async(checkpoint_path=cp_path, environment="demo")

        raised = exc_info.value
        assert raised is failure
        assert raised.line_number == 7

        from conductor.cli.app import print_error

        print_error(raised)
        rendered = capsys.readouterr().err
        assert secret not in str(raised)
        assert secret not in rendered
        assert rendered.count(REDACTED_MARKER) == 4

    @pytest.mark.asyncio
    async def test_run_oserror_structured_fields_are_scrubbed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Requirement (review fix B): run scrubs the OSError attributes used
        # by __str__ while preserving the original object, subtype, and errno.
        secret = "run-oserror-secret-3"
        monkeypatch.setenv(_CLI_TOKEN_VAR, secret)
        wf_path = _write_workflow(tmp_path)
        _write_environment(tmp_path)
        failure = FileNotFoundError(
            errno.ENOENT,
            f"missing executable {secret}",
            f"/private/{secret}/command",
        )

        from conductor.cli.run import run_workflow_async

        with (
            patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
            patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
        ):
            engine = MagicMock()
            engine.run = AsyncMock(side_effect=failure)
            engine.config.workflow.cost.show_summary = False
            _wire_registry_with_engine(mock_registry_cls, mock_engine_cls, engine)
            with pytest.raises(FileNotFoundError) as exc_info:
                await run_workflow_async(wf_path, {}, environment="demo")

        raised = exc_info.value
        assert raised is failure
        assert type(raised) is FileNotFoundError
        assert raised.errno == errno.ENOENT

        from conductor.cli.app import print_error

        print_error(raised)
        rendered = capsys.readouterr().err
        assert secret not in str(raised)
        assert secret not in rendered
        assert REDACTED_MARKER in str(raised)
        assert REDACTED_MARKER in rendered

    @pytest.mark.asyncio
    async def test_resume_two_path_oserror_structured_fields_are_scrubbed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Requirement (review fix B): resume scrubs strerror and both filenames
        # that CPython's two-path OSError renderer reads after cleanup.
        secret = "resume-oserror-secret-4"
        monkeypatch.setenv(_CLI_TOKEN_VAR, secret)
        wf_path = _write_workflow(tmp_path)
        _write_environment(tmp_path)
        cp_path = _write_checkpoint(tmp_path, wf_path)
        failure = OSError(
            errno.EXDEV,
            f"cross-device move {secret}",
            f"/source/{secret}",
            None,
            f"/destination/{secret}",
        )

        from conductor.cli.run import resume_workflow_async

        with (
            patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
            patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
            patch("conductor.cli.run._write_terminal_record_for_current_process"),
        ):
            engine = MagicMock()
            engine.resume = AsyncMock(side_effect=failure)
            engine.config.workflow.cost.show_summary = False
            _wire_registry_with_engine(mock_registry_cls, mock_engine_cls, engine)
            with pytest.raises(OSError) as exc_info:
                await resume_workflow_async(checkpoint_path=cp_path, environment="demo")

        raised = exc_info.value
        assert raised is failure
        assert type(raised) is OSError
        assert raised.errno == errno.EXDEV

        from conductor.cli.app import print_error

        print_error(raised)
        rendered = capsys.readouterr().err
        assert secret not in str(raised)
        assert secret not in rendered
        assert str(raised).count(REDACTED_MARKER) == 3
        assert rendered.count(REDACTED_MARKER) == 3

    async def test_inactive_redactor_leaves_exception_fields_byte_identical(self) -> None:
        # Requirement (review fix B): direct helper calls with an inactive
        # redactor preserve args, structured attributes, and rendered text.
        from conductor.cli.run import _scrub_exception_fields

        configuration = ConfigurationError(
            "plain message",
            suggestion="plain suggestion",
            file_path="plain/path.yaml",
            line_number=9,
            field_path="workflow.plain",
        )
        os_error = OSError(errno.EXDEV, "plain move", "plain/source", None, "plain/target")
        before = (
            configuration.args,
            configuration.suggestion,
            configuration.file_path,
            configuration.field_path,
            str(configuration),
            os_error.args,
            os_error.strerror,
            os_error.filename,
            os_error.filename2,
            str(os_error),
        )

        inactive = RunRedactor()
        _scrub_exception_fields(configuration, inactive)
        _scrub_exception_fields(os_error, inactive)

        assert (
            configuration.args,
            configuration.suggestion,
            configuration.file_path,
            configuration.field_path,
            str(configuration),
            os_error.args,
            os_error.strerror,
            os_error.filename,
            os_error.filename2,
            str(os_error),
        ) == before

    async def test_scrubs_bytes_and_nested_container_args(self) -> None:
        # Requirement (review fix B): every args element the renderer can
        # print is scrubbed, including bytes payloads and nested containers.
        from conductor.cli.run import _scrub_exception_fields

        secret = "container-secret-value"
        redactor = RunRedactor()
        redactor.register([secret])

        error = ValueError(b"prefix-" + secret.encode(), {"token": secret}, 42)

        _scrub_exception_fields(error, redactor)

        assert error.args[0] == b"prefix-" + REDACTED_MARKER.encode()
        assert error.args[1] == {"token": REDACTED_MARKER}
        assert error.args[2] == 42
        assert secret not in str(error)
        assert secret not in repr(error.args)


@pytest.mark.asyncio
async def test_script_stderr_diagnostics_are_scrubbed_real_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement (review b3): backend diagnostics carry the child's stderr,
    # so a script printing its delivered credential to stderr must not write
    # it to the verbose console / --log-file sink. Real ScriptExecutor +
    # LocalRunnerBackend end to end — the live subprocess result itself is
    # unchanged.
    secret = "stderr-secret-value-0042"
    monkeypatch.setenv(_CLI_TOKEN_VAR, secret)
    program = "import os,sys; sys.stderr.write(os.environ['DELIVERED_TOKEN'])"
    body = f"""\
workflow:
  name: cli-secrets-stderr
  entry_point: run
agents:
  - name: run
    type: script
    command: {json.dumps(sys.executable)}
    args: [{json.dumps("-c")}, {json.dumps(program)}]
    execution:
      secrets:
        - ref: token
          scope: script
          delivery:
            env: DELIVERED_TOKEN
    routes:
      - to: $end
output:
  result: "{{{{ run.output.stdout }}}}"
"""
    wf_path = _write_workflow(tmp_path, body)
    _write_environment(tmp_path)

    logged: list[str] = []

    from conductor.cli import run as run_module

    real_verbose_log = run_module.verbose_log

    def spy_verbose_log(message: Any, style: str = "dim") -> None:
        logged.append(str(message))
        return real_verbose_log(message, style)

    monkeypatch.setattr(run_module, "verbose_log", spy_verbose_log)

    from conductor.cli.run import run_workflow_async

    with patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls:
        mock_registry = AsyncMock()
        mock_registry_cls.return_value = mock_registry
        mock_registry.__aenter__ = AsyncMock(return_value=mock_registry)
        mock_registry.__aexit__ = AsyncMock(return_value=False)
        result = await run_workflow_async(wf_path, {}, environment="demo")

    rendered = "\n".join(logged)
    assert secret not in rendered
    assert REDACTED_MARKER in rendered
    # The live result is unaffected: the step ran and routed to $end.
    assert result == {"result": ""}


@pytest.mark.asyncio
async def test_run_secret_preflight_fails_before_mcp_and_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: a broken secret reference (unset source variable) fails at
    # prepare time as ConfigurationError — before _build_mcp_servers and before
    # engine construction — through the existing error path.
    monkeypatch.delenv(_CLI_TOKEN_VAR, raising=False)
    wf_path = _write_workflow(tmp_path)
    _write_environment(tmp_path)

    from conductor.cli.run import run_workflow_async

    with (
        patch("conductor.cli.run.ProviderRegistry"),
        patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
        patch("conductor.cli.run._build_mcp_servers", new_callable=AsyncMock) as mock_build,
        pytest.raises(ConfigurationError, match="not set or is empty"),
    ):
        await run_workflow_async(wf_path, {}, environment="demo")

    mock_build.assert_not_called()
    mock_engine_cls.assert_not_called()
    # Cleanup still ran on the failure path.
    assert redaction.current() is None


@pytest.mark.asyncio
async def test_resume_prepares_secrets_before_guidance_and_seeding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: on the resume path the secret prepare (emitter attach) lands
    # STRICTLY BEFORE --guidance application, the dashboard seeding (prepend +
    # replay + synthetic fallback), and the direct workflow_started JSONL write.
    monkeypatch.setenv(_CLI_TOKEN_VAR, "resume-secret-value")
    wf_path = _write_workflow(tmp_path)
    _write_environment(tmp_path)
    # A real (empty) prior event log so the replay branch is reachable.
    prior_log = tmp_path / "prior.events.jsonl"
    prior_log.write_text("", encoding="utf-8")
    cp_path = _write_checkpoint(tmp_path, wf_path, event_log_path=prior_log)

    order: list[str] = []

    real_set_redactor = WorkflowEventEmitter.set_redactor

    def spy_set_redactor(self: WorkflowEventEmitter, redactor: RunRedactor | None) -> None:
        order.append("set_redactor")
        return real_set_redactor(self, redactor)

    monkeypatch.setattr(WorkflowEventEmitter, "set_redactor", spy_set_redactor)

    engine = MagicMock()
    engine.add_user_guidance = MagicMock(side_effect=lambda *a, **k: order.append("guidance"))
    engine.build_workflow_started_data = AsyncMock(return_value={"name": "cli-secrets"})

    dashboard = MagicMock()
    dashboard.port = 8080
    dashboard.url = "http://127.0.0.1:8080"
    dashboard.start = AsyncMock()
    dashboard.stop = AsyncMock()
    dashboard.wait_for_stop = AsyncMock()
    dashboard.wait_for_clients_disconnect = AsyncMock()
    dashboard.prepend_workflow_started = MagicMock(
        side_effect=lambda *a, **k: order.append("prepend")
    )
    dashboard.replay_events_from_jsonl = MagicMock(
        side_effect=lambda *a, **k: order.append("replay") or 0
    )
    dashboard.replay_synthetic_from_context = MagicMock(
        side_effect=lambda *a, **k: order.append("synthetic") or 1
    )
    monkeypatch.setattr("conductor.web.server.WebDashboard", MagicMock(return_value=dashboard))

    from conductor.engine import event_log as event_log_mod

    real_subscriber_cls = event_log_mod.EventLogSubscriber

    def subscriber_factory(*args: Any, **kwargs: Any) -> Any:
        instance = real_subscriber_cls(*args, **kwargs)
        real_on_event = instance.on_event

        def spy_on_event(event: WorkflowEvent) -> None:
            order.append(f"direct:{event.type}")
            return real_on_event(event)

        instance.on_event = spy_on_event
        return instance

    monkeypatch.setattr(event_log_mod, "EventLogSubscriber", subscriber_factory)

    from conductor.cli.run import resume_workflow_async

    with (
        patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
        patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
        patch("conductor.cli.run._write_terminal_record_for_current_process"),
    ):
        _mock_registry_and_engine(mock_registry_cls, mock_engine_cls, "resume", engine=engine)
        await resume_workflow_async(
            checkpoint_path=cp_path,
            environment="demo",
            guidance=["keep going"],
            web=True,
            web_bg=True,
        )

    assert order[0] == "set_redactor"
    for sink in ("guidance", "prepend", "direct:workflow_started", "replay", "synthetic"):
        assert sink in order, f"missing sink observation: {sink}"
        assert order.index("set_redactor") < order.index(sink)

    # The engine received the CLI-owned pair, and CLI cleanup ran.
    kwargs = mock_engine_cls.call_args.kwargs
    assert isinstance(kwargs["secret_cache"], SecretValueCache)
    assert isinstance(kwargs["redactor"], RunRedactor)
    assert not kwargs["redactor"].active
    assert redaction.current() is None


@pytest.mark.asyncio
async def test_run_without_declared_secrets_is_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: a workflow with no declared secrets keeps the pre-secrets
    # behavior exactly — the inputs panel is printed verbatim (identity
    # passthrough) and the redactor never activates.
    wf_path = _write_workflow(tmp_path, _PLAIN_WORKFLOW)

    printed: list[str] = []

    from conductor.cli import run as run_module

    real_section = run_module.verbose_log_section

    def spy_section(title: str, content: str) -> None:
        printed.append(content)
        return real_section(title, content)

    monkeypatch.setattr(run_module, "verbose_log_section", spy_section)

    from conductor.cli.run import run_workflow_async

    with (
        patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
        patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
    ):
        _mock_registry_and_engine(mock_registry_cls, mock_engine_cls, "run")
        result = await run_workflow_async(wf_path, {"key": "plain-value"}, environment=None)

    assert result == {"result": "ok"}
    inputs_panel = json.dumps({"key": "plain-value"}, indent=2, ensure_ascii=False)
    assert printed == [inputs_panel]
    assert REDACTED_MARKER not in printed[0]
    assert redaction.current() is None


@pytest.mark.asyncio
async def test_build_mcp_servers_secrets_defaults_to_legacy() -> None:
    # Requirement: ``secrets`` is optional — omitting it (or passing None)
    # keeps the legacy server translation byte-identical.
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="mcp-default",
            entry_point="mark",
            runtime=RuntimeConfig(
                provider="copilot",
                mcp_servers={"srv": MCPServerDef(command="srv-command")},
            ),
        ),
        agents=[SetStepDef(name="mark", value="'ok'", routes=[RouteDef(to="$end")])],
    )

    legacy = await _build_mcp_servers(config)
    with_none = await _build_mcp_servers(config, secrets=None)

    assert legacy is not None
    assert legacy == with_none


def _wire_registry_with_engine(
    mock_registry_cls: MagicMock,
    mock_engine_cls: MagicMock,
    engine: MagicMock,
) -> None:
    """Wire the standard async registry mock around a pre-built engine mock.

    Unlike ``_mock_registry_and_engine`` this leaves the engine's own method
    mocks (``run`` / ``resume`` side effects) untouched.
    """
    mock_registry = AsyncMock()
    mock_registry_cls.return_value = mock_registry
    mock_registry.__aenter__ = AsyncMock(return_value=mock_registry)
    mock_registry.__aexit__ = AsyncMock(return_value=False)
    mock_engine_cls.return_value = engine


class TestFinalStdoutSink:
    """Sinks II: the final stdout boundary scrubs a copy."""

    @pytest.mark.asyncio
    async def test_final_stdout_output_is_scrubbed_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: run_workflow_async returns the final output as a
        # scrubbed COPY — every print site in cli/app.py (normal and --silent
        # paths alike) reads post-redactor values — while the engine-produced
        # dict itself is never mutated.
        secret = "final-secret-value-1"
        monkeypatch.setenv(_CLI_TOKEN_VAR, secret)
        wf_path = _write_workflow(tmp_path)
        _write_environment(tmp_path)
        engine_raw = {"result": f"prefix-{secret}-suffix"}

        from conductor.cli.run import run_workflow_async

        with (
            patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
            patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
        ):
            engine = MagicMock()
            engine.run = AsyncMock(return_value=engine_raw)
            engine.config.workflow.cost.show_summary = False
            _wire_registry_with_engine(mock_registry_cls, mock_engine_cls, engine)
            result = await run_workflow_async(wf_path, {}, environment="demo")

        rendered = json.dumps(result)
        assert secret not in rendered
        assert REDACTED_MARKER in rendered
        assert result is not engine_raw
        assert engine_raw == {"result": f"prefix-{secret}-suffix"}

    @pytest.mark.asyncio
    async def test_terminated_reraise_scrubs_rendered_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: a caught WorkflowTerminated is re-created with scrubbed
        # message/output/reason/suggestion — all Jinja-rendered from context
        # and able to carry a declared value — while terminated_by (a step
        # name, never rendered) is preserved verbatim and the original
        # exception object is not mutated.
        secret = "terminate-secret-2"
        monkeypatch.setenv(_CLI_TOKEN_VAR, secret)
        wf_path = _write_workflow(tmp_path)
        _write_environment(tmp_path)
        original = WorkflowTerminated(
            f"boom {secret}",
            output={"detail": f"out-{secret}"},
            reason=f"because {secret}",
            terminated_by="stop_here",
            suggestion=f"rotate {secret}",
        )

        from conductor.cli.run import run_workflow_async

        with (
            patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
            patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
        ):
            engine = MagicMock()
            engine.run = AsyncMock(side_effect=original)
            engine.config.workflow.cost.show_summary = False
            _wire_registry_with_engine(mock_registry_cls, mock_engine_cls, engine)
            with pytest.raises(WorkflowTerminated) as exc_info:
                await run_workflow_async(wf_path, {}, environment="demo")

        raised = exc_info.value
        assert raised is not original
        assert raised.terminated_by == "stop_here"
        for rendered in (str(raised), raised.args[0], raised.reason):
            assert secret not in rendered
            assert REDACTED_MARKER in rendered
        assert raised.suggestion is not None
        assert secret not in raised.suggestion
        assert REDACTED_MARKER in raised.suggestion
        raised_output = json.dumps(raised.output)
        assert secret not in raised_output
        assert REDACTED_MARKER in raised_output
        # The original exception keeps its raw rendered fields.
        assert original.args[0] == f"boom {secret}"
        assert original.reason == f"because {secret}"
        assert original.suggestion == f"rotate {secret}"
        assert original.output == {"detail": f"out-{secret}"}


class TestTerminalRecordTelemetrySink:
    """Sinks II: terminal record + telemetry close get copies."""

    @pytest.mark.asyncio
    async def test_terminal_record_and_telemetry_close_receive_scrubbed_copies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: the terminal run record (output + error type/message)
        # and the telemetry close arguments (telemetry_closed is written
        # outside the emitter, telemetry/subscriber.py) receive scrubbed
        # copies — scrubbed while the CLI-owned redactor is still alive —
        # without mutating the engine-produced terminal values.
        secret = "terminal-secret-3"
        monkeypatch.setenv(_CLI_TOKEN_VAR, secret)
        wf_path = _write_workflow(tmp_path)
        _write_environment(tmp_path)
        original = WorkflowTerminated(
            f"reason {secret}",
            output={"detail": secret},
            reason=f"reason {secret}",
            terminated_by="stop_here",
        )

        from conductor.cli.run import run_workflow_async

        with (
            patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
            patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
            patch("conductor.cli.run._write_terminal_record_for_current_process") as mock_record,
            patch("conductor.telemetry.subscriber.TelemetrySubscriber") as mock_telemetry_cls,
        ):
            engine = MagicMock()
            engine.run = AsyncMock(side_effect=original)
            engine.config.workflow.cost.show_summary = False
            _wire_registry_with_engine(mock_registry_cls, mock_engine_cls, engine)
            with pytest.raises(WorkflowTerminated):
                await run_workflow_async(wf_path, {}, environment="demo")

        record_kwargs = mock_record.call_args.kwargs
        record_output = json.dumps(record_kwargs["output"])
        assert secret not in record_output
        assert REDACTED_MARKER in record_output
        assert record_kwargs["error_type"] == "WorkflowTerminated"
        assert record_kwargs["error_message"] is not None
        assert secret not in record_kwargs["error_message"]
        assert REDACTED_MARKER in record_kwargs["error_message"]

        close_kwargs = mock_telemetry_cls.return_value.close.call_args.kwargs
        assert close_kwargs["error_type"] == "WorkflowTerminated"
        assert close_kwargs["error_message"] is not None
        assert secret not in close_kwargs["error_message"]
        assert REDACTED_MARKER in close_kwargs["error_message"]

        # The engine-produced values behind both sinks are untouched.
        assert original.output == {"detail": secret}
        assert original.reason == f"reason {secret}"


class TestDashboardReplaySink:
    """Sinks II: replay seeding scrubs dashboard history."""

    def test_replay_events_from_jsonl_scrubs_history_payloads(self, tmp_path: Path) -> None:
        # Requirement: payloads replayed from the prior run's JSONL log land
        # in dashboard history scrubbed of declared values; the on-disk log
        # is not rewritten (historical pre-upgrade logs stay as they are).
        from conductor.web.server import WebDashboard

        dashboard = WebDashboard(WorkflowEventEmitter(), host="127.0.0.1", port=0)
        secret = "replay-secret-4"
        redactor = RunRedactor()
        redactor.register([secret])
        dashboard.set_redactor(redactor)
        log = tmp_path / "prior.events.jsonl"
        log.write_text(
            json.dumps(
                {
                    "type": "agent_completed",
                    "timestamp": 1.0,
                    "data": {"output": f"token {secret}"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        before = log.read_bytes()

        count = dashboard.replay_events_from_jsonl(log)

        assert count == 1
        history = json.dumps(dashboard._event_history)  # noqa: SLF001
        assert secret not in history
        assert REDACTED_MARKER in history
        assert log.read_bytes() == before

    def test_replay_preserves_event_envelope_when_secret_matches_event_type(
        self, tmp_path: Path
    ) -> None:
        # Requirement (review r4): replay scrubs only the ``data`` payload and
        # preserves the envelope's ``type`` and ``timestamp`` — exactly what
        # live emission guarantees. A registered secret equal to part of an
        # event type (e.g. ``completed``) must not rewrite ``agent_completed``
        # into an undispatchable ``agent_***redacted***`` (whose positive
        # replay count would also block the synthetic-history fallback).
        from conductor.web.server import WebDashboard

        dashboard = WebDashboard(WorkflowEventEmitter(), host="127.0.0.1", port=0)
        redactor = RunRedactor()
        redactor.register(["completed"])
        dashboard.set_redactor(redactor)
        log = tmp_path / "prior.events.jsonl"
        log.write_text(
            json.dumps(
                {
                    "type": "agent_completed",
                    "timestamp": 1.0,
                    "data": {"output": "task completed"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        count = dashboard.replay_events_from_jsonl(log)

        assert count == 1
        (event,) = dashboard._event_history  # noqa: SLF001
        assert event["type"] == "agent_completed"
        assert event["timestamp"] == 1.0
        assert event["data"] == {"output": f"task {REDACTED_MARKER}"}

    def test_replay_synthetic_from_context_scrubs_agent_outputs(self) -> None:
        # Requirement: synthetic *_started/*_completed pairs carry
        # agent_outputs from the checkpoint context — a declared value in an
        # output is scrubbed before the history-append, and the context's
        # outputs are not mutated.
        from conductor.web.server import WebDashboard

        config = WorkflowConfig(
            workflow=WorkflowDef(name="synth", entry_point="mark"),
            agents=[SetStepDef(name="mark", value="'ok'", routes=[RouteDef(to="$end")])],
        )
        dashboard = WebDashboard(WorkflowEventEmitter(), host="127.0.0.1", port=0)
        secret = "synthetic-secret-5"
        redactor = RunRedactor()
        redactor.register([secret])
        dashboard.set_redactor(redactor)
        agent_outputs = {"mark": {"result": secret}}
        context = SimpleNamespace(execution_history=["mark"], agent_outputs=agent_outputs)

        count = dashboard.replay_synthetic_from_context(context, config)

        assert count == 2
        history = json.dumps(dashboard._event_history)  # noqa: SLF001
        assert secret not in history
        assert REDACTED_MARKER in history
        assert agent_outputs == {"mark": {"result": secret}}

    def test_inactive_redactor_replays_payloads_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: with an inactive redactor (or none attached) replay
        # appends payloads byte-identically — a spy on RunRedactor.scrub
        # proves zero copies were made.
        from conductor.web.server import WebDashboard

        scrub_calls: list[Any] = []
        real_scrub = RunRedactor.scrub

        def spy_scrub(self: RunRedactor, obj: Any) -> Any:
            result = real_scrub(self, obj)
            scrub_calls.append((obj, result))
            return result

        monkeypatch.setattr(RunRedactor, "scrub", spy_scrub)

        payload = {
            "type": "agent_completed",
            "timestamp": 1.0,
            "data": {"output": "token plain"},
        }
        log = tmp_path / "prior.events.jsonl"
        log.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        for redactor in (None, RunRedactor()):
            scrub_calls.clear()
            dashboard = WebDashboard(WorkflowEventEmitter(), host="127.0.0.1", port=0)
            dashboard.set_redactor(redactor)

            count = dashboard.replay_events_from_jsonl(log)

            assert count == 1
            assert scrub_calls == []
            assert dashboard._event_history == [payload]  # noqa: SLF001


class TestResumeSeedingSink:
    """Sinks II: the resume-generation workflow_started scrub."""

    @pytest.mark.asyncio
    async def test_resume_direct_workflow_started_write_is_scrubbed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: the resume-generation workflow_started is prepended to
        # dashboard history and written directly to the JSONL log AND the
        # telemetry subscriber — all three bypass the emitter, so the payload
        # is scrubbed once up front; the dict the engine built is not mutated.
        secret = "resume-seed-secret-6"
        monkeypatch.setenv(_CLI_TOKEN_VAR, secret)
        wf_path = _write_workflow(tmp_path)
        _write_environment(tmp_path)
        prior_log = tmp_path / "prior.events.jsonl"
        prior_log.write_text("", encoding="utf-8")
        cp_path = _write_checkpoint(tmp_path, wf_path, event_log_path=prior_log)
        started_data = {"name": "cli-secrets", "inputs": {"token": secret}}

        engine = MagicMock()
        engine.add_user_guidance = MagicMock()
        engine.build_workflow_started_data = AsyncMock(return_value=started_data)
        engine.resume = AsyncMock(return_value={"result": "ok"})

        dashboard = MagicMock()
        dashboard.port = 8080
        dashboard.url = "http://127.0.0.1:8080"
        dashboard.start = AsyncMock()
        dashboard.stop = AsyncMock()
        dashboard.wait_for_stop = AsyncMock()
        dashboard.wait_for_clients_disconnect = AsyncMock()
        dashboard.replay_events_from_jsonl = MagicMock(return_value=1)
        monkeypatch.setattr("conductor.web.server.WebDashboard", MagicMock(return_value=dashboard))

        direct_writes: list[WorkflowEvent] = []
        telemetry_writes: list[WorkflowEvent] = []

        from conductor.engine import event_log as event_log_mod

        real_subscriber_cls = event_log_mod.EventLogSubscriber

        def subscriber_factory(*args: Any, **kwargs: Any) -> Any:
            instance = real_subscriber_cls(*args, **kwargs)
            real_on_event = instance.on_event

            def spy_on_event(event: WorkflowEvent) -> None:
                if event.type == "workflow_started":
                    direct_writes.append(event)
                return real_on_event(event)

            instance.on_event = spy_on_event
            return instance

        monkeypatch.setattr(event_log_mod, "EventLogSubscriber", subscriber_factory)

        def spy_telemetry(event: WorkflowEvent) -> None:
            if event.type == "workflow_started":
                telemetry_writes.append(event)

        from conductor.cli.run import resume_workflow_async

        with (
            patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
            patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
            patch("conductor.cli.run._write_terminal_record_for_current_process"),
            patch("conductor.telemetry.subscriber.TelemetrySubscriber") as mock_telemetry_cls,
        ):
            mock_telemetry_cls.return_value.on_event.side_effect = spy_telemetry
            _wire_registry_with_engine(mock_registry_cls, mock_engine_cls, engine)
            await resume_workflow_async(
                checkpoint_path=cp_path,
                environment="demo",
                web=True,
                web_bg=True,
            )

        assert len(direct_writes) == 1
        assert len(telemetry_writes) == 1
        for event in (*direct_writes, *telemetry_writes):
            rendered = json.dumps(event.data)
            assert secret not in rendered
            assert REDACTED_MARKER in rendered
        prepended = dashboard.prepend_workflow_started.call_args.args[0]
        prepended_rendered = json.dumps(prepended)
        assert secret not in prepended_rendered
        assert REDACTED_MARKER in prepended_rendered
        # The dict the engine built is untouched.
        assert started_data == {"name": "cli-secrets", "inputs": {"token": secret}}


class TestInactiveSinkParity:
    """Sinks II: an inactive redactor keeps byte parity."""

    @pytest.mark.asyncio
    async def test_inactive_redactor_returns_engine_output_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: with no declared secrets the redactor never activates:
        # the returned output is the very object the engine produced, the
        # terminal record receives that same object, and a spy on
        # RunRedactor.scrub proves no scrub call happens at all — every sink
        # short-circuits on the active check (zero copies) — so the
        # pre-secrets behavior is byte-identical.
        wf_path = _write_workflow(tmp_path, _PLAIN_WORKFLOW)
        engine_raw = {"result": "ok"}
        scrub_calls: list[tuple[Any, Any]] = []
        real_scrub = RunRedactor.scrub

        def spy_scrub(self: RunRedactor, obj: Any) -> Any:
            result = real_scrub(self, obj)
            scrub_calls.append((obj, result))
            return result

        monkeypatch.setattr(RunRedactor, "scrub", spy_scrub)

        from conductor.cli.run import run_workflow_async

        with (
            patch("conductor.cli.run.ProviderRegistry") as mock_registry_cls,
            patch("conductor.cli.run.WorkflowEngine") as mock_engine_cls,
            patch("conductor.cli.run._write_terminal_record_for_current_process") as mock_record,
        ):
            engine = MagicMock()
            engine.run = AsyncMock(return_value=engine_raw)
            engine.config.workflow.cost.show_summary = False
            _wire_registry_with_engine(mock_registry_cls, mock_engine_cls, engine)
            result = await run_workflow_async(wf_path, {"key": "plain"}, environment=None)

        assert result is engine_raw
        assert not scrub_calls
        record_kwargs = mock_record.call_args.kwargs
        assert record_kwargs["status"] == "success"
        assert record_kwargs["output"] is engine_raw
        assert record_kwargs["error_type"] is None
        assert record_kwargs["error_message"] is None
