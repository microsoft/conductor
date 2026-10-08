"""Tests for checkpoint payload secret redaction (secrets-contract, sinks I).

Covers ``CheckpointManager.save_checkpoint(..., redactor=...)``: registered
secret values must be scrubbed from the persisted checkpoint file while the
live ``WorkflowContext`` and the caller's ``inputs`` mapping keep the original
values, and a ``None``/inactive redactor must preserve byte-for-byte the
pre-change output.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from conductor.engine.checkpoint import CheckpointManager
from conductor.engine.context import WorkflowContext
from conductor.engine.limits import LimitEnforcer
from conductor.redaction import REDACTED_MARKER, RunRedactor

# ---------------------------------------------------------------------------
# Helpers (mirroring tests/test_engine/test_checkpoint.py conventions)
# ---------------------------------------------------------------------------


def _make_context(
    inputs: dict[str, Any] | None = None,
    agents: dict[str, dict[str, Any]] | None = None,
) -> WorkflowContext:
    """Build a WorkflowContext with optional inputs and agent outputs."""
    ctx = WorkflowContext()
    if inputs:
        ctx.set_workflow_inputs(inputs)
    if agents:
        for name, output in agents.items():
            ctx.store(name, output)
    return ctx


def _make_limits(
    iterations: int = 0,
    max_iter: int = 10,
    history: list[str] | None = None,
) -> LimitEnforcer:
    """Build a LimitEnforcer with iteration state."""
    enforcer = LimitEnforcer(max_iterations=max_iter, timeout_seconds=300)
    enforcer.start()
    enforcer.current_iteration = iterations
    enforcer.execution_history = list(history or [])
    return enforcer


def _write_workflow(tmp_path: Path, content: str = "name: test-workflow\n") -> Path:
    """Write a dummy workflow YAML and return its path."""
    wf = tmp_path / "workflow.yaml"
    wf.write_text(content)
    return wf


def _save(tmp_path: Path, *, redactor: RunRedactor | None = None, **overrides: Any) -> Path:
    """Save a checkpoint into *tmp_path* with sensible defaults plus overrides."""
    wf = overrides.pop("workflow_path", _write_workflow(tmp_path))
    ctx = overrides.pop("context", _make_context({"q": "hi"}, {"agent_a": {"answer": "yes"}}))
    limits = overrides.pop("limits", _make_limits(1, 10, ["agent_a"]))
    error = overrides.pop("error", RuntimeError("boom"))
    inputs = overrides.pop("inputs", {"q": "hi"})
    system_metadata = overrides.pop("system_metadata", None)
    instructions_preamble = overrides.pop("instructions_preamble", None)

    with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
        path = CheckpointManager.save_checkpoint(
            wf,
            ctx,
            limits,
            overrides.pop("current_agent", "agent_b"),
            error,
            inputs,
            system_metadata=system_metadata,
            instructions_preamble=instructions_preamble,
            redactor=redactor,
        )
    assert path is not None
    return path


# ---------------------------------------------------------------------------
# save_checkpoint redaction tests
# ---------------------------------------------------------------------------


class TestSaveCheckpointRedaction:
    """Requirement: registered secrets are scrubbed from the checkpoint file
    only — the live engine context and caller inputs keep the raw values."""

    def test_context_output_secret_scrubbed_in_file_but_live_context_keeps_value(
        self, tmp_path: Path
    ) -> None:
        # Requirement: a registered value inside a context agent output appears
        # as the marker in the file while the live WorkflowContext still holds it.
        secret = "ctx-secret-token"
        ctx = _make_context(
            {"question": "plain"},
            {"agent_a": {"answer": f"the answer uses {secret}"}},
        )
        redactor = RunRedactor()
        redactor.register([secret])

        path = _save(tmp_path, context=ctx, redactor=redactor)

        raw = path.read_text()
        assert secret not in raw
        assert REDACTED_MARKER in raw
        # Live context is not mutated: the raw value is still there.
        assert ctx.agent_outputs["agent_a"]["answer"] == f"the answer uses {secret}"

        data = json.loads(raw)
        assert data["context"]["agent_outputs"]["agent_a"]["answer"] == (
            f"the answer uses {REDACTED_MARKER}"
        )

    def test_inputs_secret_scrubbed_in_file_but_live_inputs_kept(self, tmp_path: Path) -> None:
        # Requirement: a registered value inside the top-level inputs payload is
        # scrubbed in the file while the caller's inputs dict keeps the raw value.
        secret = "input-secret-token"
        inputs = {"api_key": secret, "question": "plain"}
        redactor = RunRedactor()
        redactor.register([secret])

        path = _save(tmp_path, inputs=inputs, redactor=redactor)

        raw = path.read_text()
        assert secret not in raw
        assert json.loads(raw)["inputs"]["api_key"] == REDACTED_MARKER
        # The caller's mapping is not mutated.
        assert inputs["api_key"] == secret

    def test_failure_message_secret_scrubbed(self, tmp_path: Path) -> None:
        # Requirement: a registered value inside failure.message is scrubbed in
        # the file while the exception object itself is untouched.
        secret = "failure-secret-token"
        error = RuntimeError(f"provider exploded with {secret}")
        redactor = RunRedactor()
        redactor.register([secret])

        path = _save(tmp_path, error=error, redactor=redactor)

        data = json.loads(path.read_text())
        assert data["failure"]["message"] == f"provider exploded with {REDACTED_MARKER}"
        assert str(error) == f"provider exploded with {secret}"

    def test_system_metadata_and_instructions_preamble_scrubbed(self, tmp_path: Path) -> None:
        # Requirement: secrets inside system metadata and the instructions
        # preamble are scrubbed from the persisted payload.
        secret = "preamble-secret-token"
        redactor = RunRedactor()
        redactor.register([secret])

        path = _save(
            tmp_path,
            system_metadata={"env_note": f"token was {secret}"},
            instructions_preamble=f"# Workspace rules\nUse {secret} carefully.\n",
            redactor=redactor,
        )

        data = json.loads(path.read_text())
        assert data["system"]["env_note"] == f"token was {REDACTED_MARKER}"
        assert data["instructions_preamble"] == (
            f"# Workspace rules\nUse {REDACTED_MARKER} carefully.\n"
        )

    def test_scrubbed_checkpoint_still_loads(self, tmp_path: Path) -> None:
        # Requirement: a redacted checkpoint remains a valid checkpoint the
        # loader accepts (key set unchanged — only string values replaced).
        secret = "loadable-secret-token"
        redactor = RunRedactor()
        redactor.register([secret])

        path = _save(
            tmp_path,
            context=_make_context({"api_key": secret}, {"agent_a": {"answer": secret}}),
            inputs={"api_key": secret},
            error=RuntimeError(f"boom {secret}"),
            redactor=redactor,
        )

        loaded = CheckpointManager.load_checkpoint(path)
        assert loaded.inputs["api_key"] == REDACTED_MARKER
        assert loaded.context["agent_outputs"]["agent_a"]["answer"] == REDACTED_MARKER
        assert loaded.failure["message"] == f"boom {REDACTED_MARKER}"

    def test_inactive_redactor_output_byte_identical_to_no_redactor(self, tmp_path: Path) -> None:
        # Requirement: with no secrets registered the redactor performs zero
        # work — the file is byte-identical to a save without any redactor.
        secret = "idle-secret-token"
        payload = {
            "context": _make_context({"api_key": secret}, {"agent_a": {"answer": secret}}),
            "inputs": {"api_key": secret},
            "error": RuntimeError(f"boom {secret}"),
            "system_metadata": {"note": f"uses {secret}"},
            "instructions_preamble": f"preamble {secret}",
        }

        # Pin every entropy/timestamp source so two saves are byte-comparable.
        # The two saves must still land in distinct files: on Windows rename()
        # does not overwrite an existing destination, so reusing one filename
        # would make the second save fail-open to None. The filename suffix is
        # not part of the serialized payload, so byte parity is unaffected.
        fixed_now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return fixed_now

        with (
            patch("secrets.token_hex", side_effect=["aabbccdd", "eeff0011"]),
            patch("time.strftime", return_value="20260101-000000"),
            patch("conductor.engine.checkpoint.datetime", _FixedDatetime),
        ):
            path_none = _save(tmp_path, redactor=None, **payload)
            bytes_none = path_none.read_bytes()

            inactive = RunRedactor()
            path_inactive = _save(tmp_path, redactor=inactive, **payload)
            bytes_inactive = path_inactive.read_bytes()

        assert bytes_none == bytes_inactive
        # And the raw secret is present: no scrubbing happened at all.
        assert secret.encode() in bytes_none

    def test_secret_named_workflow_preserves_protocol_keys_and_path(self, tmp_path: Path) -> None:
        # Requirement (review b5): a registered secret equal to "workflow"
        # must not rename protocol keys — scrubbing ``workflow_path`` would
        # make the checkpoint unloadable while the save still reported
        # success. Keys survive verbatim; the path value (which does not
        # contain the secret) survives; a value equal to the secret is
        # scrubbed.
        secret = "workflow"
        # The workflow file lives outside tmp_path (whose pytest-generated
        # directory name contains the secret) so the path VALUE itself stays
        # secret-free and the field can be asserted verbatim.
        wf = tmp_path.parent / "app-run" / "app.yaml"
        wf.parent.mkdir(exist_ok=True)
        wf.write_text("name: app\n", encoding="utf-8")
        ctx = _make_context({"q": "hi"}, {"agent_a": {"answer": "yes"}})
        redactor = RunRedactor()
        redactor.register([secret])

        path = _save(
            tmp_path,
            workflow_path=wf,
            context=ctx,
            inputs={"q": "hi"},
            error=RuntimeError("boom"),
            redactor=redactor,
        )

        data = json.loads(path.read_text())
        assert "workflow_path" in data
        assert data["workflow_path"] == str(wf.resolve())
        assert "agent_outputs" in data["context"]
        loaded = CheckpointManager.load_checkpoint(path)
        assert loaded.workflow_path == str(wf.resolve())
        assert loaded.context["agent_outputs"]["agent_a"]["answer"] == "yes"

    def test_secret_named_iteration_preserves_counters_and_limits(self, tmp_path: Path) -> None:
        # Requirement (review b5): a registered secret equal to "iteration"
        # must not rename the iteration-related keys — the deserializer would
        # otherwise silently reset restored counters and limits to defaults.
        # Keys survive; counter values survive; a string value CONTAINING the
        # secret is scrubbed; restore keeps the counters.
        secret = "iteration"
        ctx = _make_context({"q": "hi"}, {"agent_a": {"answer": f"ran at {secret} 3"}})
        ctx.current_iteration = 4
        limits = _make_limits(4, 25, ["agent_a"])
        redactor = RunRedactor()
        redactor.register([secret])

        path = _save(tmp_path, context=ctx, limits=limits, redactor=redactor)

        data = json.loads(path.read_text())
        assert data["failure"]["iteration"] == 4
        assert data["limits"]["current_iteration"] == 4
        assert data["limits"]["max_iterations"] == 25
        assert data["context"]["current_iteration"] == 4
        assert data["context"]["agent_outputs"]["agent_a"]["answer"] == (
            f"ran at {REDACTED_MARKER} 3"
        )

        loaded = CheckpointManager.load_checkpoint(path)
        restored_ctx = WorkflowContext.from_dict(loaded.context)
        assert restored_ctx.current_iteration == 4
        restored_limits = LimitEnforcer.from_dict(
            loaded.limits,
            timeout_seconds=300,
            budget_usd=None,
            budget_mode="audit",
        )
        assert restored_limits.current_iteration == 4
        assert restored_limits.max_iterations == 25

    def test_execution_manifest_structure_survives_secret_named_like_its_keys(
        self, tmp_path: Path
    ) -> None:
        # Requirement (review b5, oracle round): inside
        # ``system.execution_manifest`` the ENTIRE key set is the fixed
        # ResolvedRunManifest schema — including the dynamic profile identity
        # keys (executable step names) and the secret audit rows. A
        # registered secret equal to one of those key names (``profile``,
        # ``environment``, ``ref``, ``scope``) or to a step name must not
        # rename any key: the persisted audit manifest must stay
        # structurally valid and re-parse.
        from conductor.config.environment import (
            EnvironmentDocument,
            ProfileDefinition,
            ResolvedEnvironment,
            SecretBinding,
            SecretBindingSource,
        )
        from conductor.config.schema import (
            RouteDef,
            ScriptStepDef,
            SecretDelivery,
            StepExecutionConfig,
            StepSecretRef,
            WorkflowConfig,
            WorkflowDef,
        )
        from conductor.engine.run_manifest import ResolvedRunManifest, compile_run_manifest

        environment = ResolvedEnvironment(
            document=EnvironmentDocument(
                default="default",
                profiles={"default": ProfileDefinition(backend="local")},
                secrets={"alpha": SecretBinding(source=SecretBindingSource(env="SRC_ALPHA"))},
            ),
            name="test-env",
            source="path",
            path=None,
            digest="sha256:test",
        )
        config = WorkflowConfig(
            workflow=WorkflowDef(name="audit-manifest", entry_point="worker"),
            agents=[
                ScriptStepDef(
                    name="worker",
                    command="echo",
                    execution=StepExecutionConfig(
                        secrets=[
                            StepSecretRef(
                                ref="alpha",
                                scope="script",
                                delivery=SecretDelivery(env="TOKEN"),
                            )
                        ]
                    ),
                    routes=[RouteDef(to="$end")],
                )
            ],
        )
        manifest = compile_run_manifest(config, workflow_path=None, environment=environment)
        redactor = RunRedactor()
        redactor.register(["profile", "environment", "ref", "scope", "worker"])

        path = _save(
            tmp_path,
            system_metadata={"execution_manifest": manifest.model_dump(mode="json")},
            redactor=redactor,
        )

        data = json.loads(path.read_text())
        parsed = ResolvedRunManifest.model_validate(data["system"]["execution_manifest"])
        # Dynamic profile identity key (the step name) survived.
        assert "worker" in parsed.profiles
        assert parsed.profiles["worker"].profile == "default"
        # The secret audit row kept its fixed key names.
        (use,) = parsed.secrets
        assert use.ref == "alpha"
        assert use.scope == "script"
        assert use.delivery_kind == "env"
        assert use.delivery_name == "TOKEN"
        # The environment block kept its key names.
        assert parsed.environment.name == "test-env"

    def test_none_redactor_leaves_raw_secret_in_file(self, tmp_path: Path) -> None:
        # Requirement: the default (redactor=None) keeps the pre-change behavior —
        # registered-or-not, nothing is scrubbed without an attached redactor.
        secret = "raw-secret-token"
        path = _save(
            tmp_path,
            context=_make_context({"api_key": secret}),
            redactor=None,
        )
        data = json.loads(path.read_text())
        assert data["context"]["workflow_inputs"]["api_key"] == secret


class TestLifecycleProtocolKeysRedaction:
    def test_lifecycle_key_names_survive_while_values_are_scrubbed(self, tmp_path: Path) -> None:
        # Requirement: lifecycle envelope keys survive secrets matching their names;
        # secret values in those blocks are scrubbed without changing digests.
        redactor = RunRedactor()
        redactor.register(["workspace", "resume_contract", "interrupted_step"])
        wf = _write_workflow(tmp_path)
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            path = CheckpointManager.save_checkpoint(
                wf,
                _make_context(),
                _make_limits(),
                "build",
                RuntimeError("stopped"),
                {},
                workspace={
                    "policy": "ephemeral",
                    "identities": {},
                    "executed_backends": [],
                    "note": "workspace",
                },
                resume_contract={
                    "workflow_digest": "sha256:abc",
                    "environment_name": "dev",
                    "environment_digest": "sha256:def",
                    "manifest_digest": "sha256:ghi",
                    "bundle_digest": None,
                    "note": "resume_contract",
                },
                interrupted_step={
                    "name": "build",
                    "status": "unknown",
                    "attempt_id": "one",
                    "note": "interrupted_step",
                },
                redactor=redactor,
            )
        assert path is not None
        data = json.loads(path.read_text())
        for key in ("workspace", "resume_contract", "interrupted_step"):
            assert data[key]["note"] == REDACTED_MARKER
        assert data["resume_contract"]["workflow_digest"] == "sha256:abc"
        assert data["resume_contract"]["manifest_digest"] == "sha256:ghi"
        loaded = CheckpointManager.load_checkpoint(path)
        assert loaded.interrupted_step is not None
        assert loaded.interrupted_step["status"] == "unknown"

    def test_digest_valued_secret_scrubs_values_but_preserves_lifecycle_keys(
        self, tmp_path: Path
    ) -> None:
        # Requirement: digest-valued secrets scrub values, not lifecycle keys.
        secret = "sha256:abc123secret"
        redactor = RunRedactor()
        redactor.register([secret])
        wf = _write_workflow(tmp_path)
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):
            path = CheckpointManager.save_checkpoint(
                wf,
                _make_context({"digest": secret}),
                _make_limits(),
                "build",
                RuntimeError("stopped"),
                {"digest": secret},
                workspace={
                    "policy": "durable",
                    "identities": {
                        "docker": {
                            "backend": "docker",
                            "lease_id": "run",
                            "incarnation": secret,
                        }
                    },
                    "executed_backends": [],
                },
                resume_contract={
                    "workflow_digest": secret,
                    "environment_name": "dev",
                    "environment_digest": "sha256:environment",
                    "manifest_digest": "sha256:manifest",
                    "bundle_digest": None,
                },
                interrupted_step={"name": "build", "status": "unknown", "attempt_id": secret},
                redactor=redactor,
            )
        assert path is not None
        data = json.loads(path.read_text())
        assert {"workspace", "resume_contract", "interrupted_step"} <= data.keys()
        assert data["resume_contract"]["workflow_digest"] == REDACTED_MARKER
        assert data["workspace"]["identities"]["docker"]["incarnation"] == REDACTED_MARKER
        assert data["interrupted_step"]["attempt_id"] == REDACTED_MARKER
        assert data["context"]["workflow_inputs"]["digest"] == REDACTED_MARKER
        assert secret not in path.read_text()
