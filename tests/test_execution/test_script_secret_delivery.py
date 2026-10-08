"""Script-step secret delivery: resolved values reach the command environment.

Covers the delivery half of the secrets wiring: a declared ``execution.secrets``
env delivery on a script step lands in the backend's ``CommandSpec.env``, the
compiled ``inherit_control_environment`` policy reaches the spec, and a
repeated ``run()`` on one self-owned engine re-resolves rotated source values.
"""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from conductor import redaction
from conductor.config.environment import (
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
    SecretBinding,
    SecretBindingSource,
)
from conductor.config.schema import (
    ContextConfig,
    LimitsConfig,
    RouteDef,
    RuntimeConfig,
    ScriptStepDef,
    SecretDelivery,
    StepExecutionConfig,
    StepSecretRef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.execution_resolution import ExecutionResolverSession
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ConfigurationError
from conductor.execution import (
    CommandResult,
    CommandSpec,
    RunnerCapabilities,
    RunOutcome,
    RunSpec,
    WorkspaceLease,
)
from conductor.execution.local import LocalRunnerBackend
from conductor.executor.script import ScriptExecutor

_TOKEN_VAR = "CONDUCTOR_TEST_TASK7_TOKEN"
_SENTINEL_VAR = "CONDUCTOR_TEST_TASK7_SENTINEL"


class RecordingBackend:
    """Batch-capable backend capturing every CommandSpec it receives."""

    def __init__(self) -> None:
        self.prepare_calls: list[RunSpec] = []
        self.run_calls: list[tuple[CommandSpec, WorkspaceLease | None]] = []
        self.finalize_calls: list[tuple[WorkspaceLease, RunOutcome]] = []
        self.lease = WorkspaceLease("lease", "local", "one")
        # Probes evaluated inside ``run_command`` (i.e. mid-run, in the
        # engine's async context): populated per call.
        self.current_redactor_active: list[bool] = []

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            batch=True,
            sessions=False,
            shared_workspace=True,
            snapshots=False,
        )

    async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
        self.prepare_calls.append(run)
        return self.lease

    async def run_command(
        self,
        spec: CommandSpec,
        lease: WorkspaceLease | None,
        *,
        diagnostics: Any = None,
        on_dispatch: Any = None,
    ) -> CommandResult:
        del diagnostics
        if on_dispatch is not None:
            on_dispatch()
        self.run_calls.append((spec, lease))
        current = redaction.current()
        self.current_redactor_active.append(current is not None and current.active)
        return CommandResult(
            outcome="completed",
            stdout=f"{spec.command}\n",
            stderr="",
            exit_code=0,
            resolved_command=spec.command,
            duration_seconds=0.0,
        )

    async def finalize_run(
        self, lease: WorkspaceLease, outcome: RunOutcome, *, retain: bool = False
    ) -> None:
        self.finalize_calls.append((lease, outcome))


def _secret_environment(*, inherit: bool | None = None) -> ResolvedEnvironment:
    """An authored environment with one env-sourced ``token`` binding."""
    document = EnvironmentDocument(
        default="default",
        profiles={
            "default": ProfileDefinition(backend="local", inherit_control_environment=inherit)
        },
        secrets={"token": SecretBinding(source=SecretBindingSource(env=_TOKEN_VAR))},
    )
    return ResolvedEnvironment(
        document=document, name="test", source="path", path=None, digest="sha256:test"
    )


def _secret_script_config(*, env: dict[str, str] | None = None) -> WorkflowConfig:
    """A single script step declaring one env-delivered script-scoped secret."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="secret-delivery",
            entry_point="run",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            ScriptStepDef(
                name="run",
                command="run-command",
                env=env or {},
                execution=StepExecutionConfig(
                    secrets=[
                        StepSecretRef(
                            ref="token",
                            scope="script",
                            delivery=SecretDelivery(env="DELIVERED_TOKEN"),
                        )
                    ]
                ),
                routes=[RouteDef(to="$end")],
            )
        ],
        output={"result": "{{ run.output.stdout }}"},
    )


def _engine(
    config: WorkflowConfig,
    backend: RecordingBackend,
    environment: ResolvedEnvironment,
) -> tuple[WorkflowEngine, ExecutionResolverSession]:
    """An engine over an injected (self-owned-secrets) session."""
    session = ExecutionResolverSession(environment, backend)
    return (
        WorkflowEngine(config, MagicMock(), _execution_session=session),
        session,
    )


@pytest.mark.asyncio
async def test_declared_secret_lands_in_command_spec_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: a declared script-scoped env delivery reaches CommandSpec.env
    # with the resolved plaintext value.
    monkeypatch.setenv(_TOKEN_VAR, "s3cr3t-value-0001")
    backend = RecordingBackend()
    engine, _session = _engine(_secret_script_config(), backend, _secret_environment())

    await engine.run({})

    ((spec, _lease),) = backend.run_calls
    assert spec.env["DELIVERED_TOKEN"] == "s3cr3t-value-0001"


@pytest.mark.asyncio
async def test_run_redactor_is_active_inside_execution_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: the engine activates the run redactor through the contextvar
    # for the duration of step execution (direct/self-owned construction).
    monkeypatch.setenv(_TOKEN_VAR, "s3cr3t-value-0002")
    backend = RecordingBackend()
    engine, session = _engine(_secret_script_config(), backend, _secret_environment())

    await engine.run({})

    assert backend.current_redactor_active == [True]
    # Self-owned session: finalize clears the run's secret state.
    assert not session.redactor.active


@pytest.mark.asyncio
async def test_inherit_control_environment_false_reaches_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: the manifest's effective inherit policy is threaded into the
    # CommandSpec (explicit profile value ``false`` wins over the local default).
    monkeypatch.setenv(_TOKEN_VAR, "s3cr3t-value-0003")
    backend = RecordingBackend()
    engine, _session = _engine(_secret_script_config(), backend, _secret_environment(inherit=False))

    await engine.run({})

    ((spec, _lease),) = backend.run_calls
    assert spec.inherit_control_environment is False
    assert spec.env["DELIVERED_TOKEN"] == "s3cr3t-value-0003"


@pytest.mark.asyncio
async def test_inherit_control_environment_false_runs_minimal_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: effective inherit false yields exactly
    # ``{"PYTHONUTF8": "1", **env}`` — no control-process os.environ leaks in.
    monkeypatch.setenv(_SENTINEL_VAR, "must-not-leak")
    backend = LocalRunnerBackend()
    spec = CommandSpec(
        command=sys.executable,
        args=("-c", "import json, os; print(json.dumps(sorted(os.environ)))"),
        env={"DELIVERED_TOKEN": "s3cr3t"},
        inherit_control_environment=False,
    )

    result = await backend.run_command(spec, None)

    assert result.outcome == "completed"
    keys = set(json.loads(result.stdout))
    assert _SENTINEL_VAR not in keys
    # ``LC_CTYPE`` is excluded: CPython adds it to its OWN os.environ via
    # PEP 538 locale coercion — it is not inherited from the control process.
    assert keys - {"LC_CTYPE"} == {"PYTHONUTF8", "DELIVERED_TOKEN"}

    inherit_result = await backend.run_command(
        CommandSpec(
            command=sys.executable,
            args=("-c", f"import os; print(os.environ.get('{_SENTINEL_VAR}', ''))"),
        ),
        None,
    )
    assert inherit_result.stdout.strip() == "must-not-leak"


@pytest.mark.asyncio
async def test_repeated_run_on_one_engine_re_resolves_rotated_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: a self-owned session resets and rebuilds its secret state per
    # run generation — a rotated source variable serves the NEW value on the
    # second run, not the cached first one.
    monkeypatch.setenv(_TOKEN_VAR, "first-value-00001")
    backend = RecordingBackend()
    engine, session = _engine(_secret_script_config(), backend, _secret_environment())

    await engine.run({})
    assert backend.run_calls[-1][0].env["DELIVERED_TOKEN"] == "first-value-00001"

    monkeypatch.setenv(_TOKEN_VAR, "second-value-0002")
    await engine.run({})
    assert backend.run_calls[-1][0].env["DELIVERED_TOKEN"] == "second-value-0002"
    # Both generations cleaned up after themselves.
    assert not session.redactor.active


@pytest.mark.asyncio
async def test_secret_value_is_never_template_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: neither secret deliveries nor declared ``env`` values are
    # Jinja-rendered — a value containing ``{{`` reaches the child verbatim.
    monkeypatch.setenv(_TOKEN_VAR, "{{ not-a-template }}-value")
    backend = RecordingBackend()
    engine, _session = _engine(
        _secret_script_config(env={"LITERAL": "{{ 1 + 1 }}"}),
        backend,
        _secret_environment(),
    )

    await engine.run({})

    ((spec, _lease),) = backend.run_calls
    assert spec.env["DELIVERED_TOKEN"] == "{{ not-a-template }}-value"
    assert spec.env["LITERAL"] == "{{ 1 + 1 }}"


@pytest.mark.asyncio
async def test_executor_defaults_are_back_compatible() -> None:
    # Requirement: without the new kwargs the executor keeps the legacy
    # behavior — declared env verbatim, control environment inherited.
    backend = RecordingBackend()
    executor = ScriptExecutor(backend)
    step = ScriptStepDef(
        name="run",
        command="run-command",
        env={"AUTHORED": "value"},
        routes=[RouteDef(to="$end")],
    )

    await executor.execute(step, {})

    ((spec, _lease),) = backend.run_calls
    assert spec.env == {"AUTHORED": "value"}
    assert spec.inherit_control_environment is True


@pytest.mark.asyncio
async def test_explicit_secret_env_kwarg_merges_into_spec() -> None:
    # Requirement: ``secret_env`` merges on top of declared ``env`` in the
    # CommandSpec the backend receives.
    backend = RecordingBackend()
    executor = ScriptExecutor(backend)
    step = ScriptStepDef(
        name="run",
        command="run-command",
        env={"AUTHORED": "value"},
        routes=[RouteDef(to="$end")],
    )

    await executor.execute(step, {}, secret_env={"DELIVERED": "s3cr3t"})

    ((spec, _lease),) = backend.run_calls
    assert spec.env == {"AUTHORED": "value", "DELIVERED": "s3cr3t"}


@pytest.mark.asyncio
async def test_session_secret_state_cleared_by_self_owned_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: finalize_leases clears cache and redactor only when the
    # session owns the pair (direct construction safety net).
    monkeypatch.setenv(_TOKEN_VAR, "s3cr3t-value-0004")
    session = ExecutionResolverSession(_secret_environment(), RecordingBackend())
    assert session.owns_secrets is True
    cache = session.secret_cache
    cache.resolve("token", consumer_class="script", consumer_label="step 'run'")
    assert session.redactor.active

    await session.finalize_leases("succeeded")

    assert not session.redactor.active
    with pytest.raises(ConfigurationError, match="has not been resolved"):
        cache.secret_for("token")


def test_reserved_for_mcp_consumers_not_served_to_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: the binding's ``allow`` policy is enforced at index time —
    # a script-scoped use of an mcp-only binding fails engine construction.
    monkeypatch.setenv(_TOKEN_VAR, "s3cr3t-value-0005")
    document = EnvironmentDocument(
        default="default",
        profiles={"default": ProfileDefinition(backend="local")},
        secrets={"token": SecretBinding(source=SecretBindingSource(env=_TOKEN_VAR), allow=["mcp"])},
    )
    environment = ResolvedEnvironment(
        document=document, name="test", source="path", path=None, digest="sha256:test"
    )
    session = ExecutionResolverSession(environment, RecordingBackend())

    with pytest.raises(ConfigurationError, match="allows only"):
        WorkflowEngine(_secret_script_config(), MagicMock(), _execution_session=session)
