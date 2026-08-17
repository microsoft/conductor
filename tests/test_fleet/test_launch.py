"""Tests for the Fleet Manager's launch primitives (``conductor.fleet.launch``,
Fleet Manager E12).

Covers:

- ``resolve_workflow``: both file and registry-reference forms (E12-T1),
  including a nonexistent file and a broken registry reference surfacing a
  ``LaunchError`` rather than a raw exception.
- ``coerce_input_value`` / ``build_launch_inputs``: type coercion for all
  five ``InputDef`` types, required-field rejection, default pre-filling,
  and a bad value for each type surfacing a ``LaunchError`` rather than
  raising the underlying ``ValueError``/``json.JSONDecodeError``.
- ``launch_workflow``: builds the right ``launch_background()`` kwargs
  (never a subprocess argv -- E12-T2 explicitly forbids re-implementing
  detached spawning), and any failure from ``launch_background()`` --
  including the D2 run-record-poll timeout -- is wrapped in a
  ``LaunchError`` carrying a plain message, not a traceback.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.config.schema import InputDef
from conductor.fleet.launch import (
    LaunchError,
    ResolvedWorkflow,
    build_launch_inputs,
    coerce_input_value,
    launch_workflow,
    resolve_workflow,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_WORKFLOW_YAML = """\
workflow:
  name: test-workflow
  description: A minimal workflow for launch tests
  entry_point: helper
  input:
    question:
      type: string
      required: true
      description: The question to answer
    verbose:
      type: boolean
      required: false
      default: false
      description: Enable verbose output
    retries:
      type: number
      required: false
      default: 3

agents:
  - name: helper
    model: copilot
    prompt: Say hello
"""


@pytest.fixture()
def workflow_file(tmp_path: Path) -> Path:
    path = tmp_path / "workflow.yaml"
    path.write_text(_WORKFLOW_YAML)
    return path


def _registry_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    return home


def _write_registry(tmp_path: Path) -> Path:
    from ruamel.yaml import YAML

    registry_dir = tmp_path / "registry"
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "workflow.yaml").write_text(_WORKFLOW_YAML)

    yaml = YAML()
    index_data = {
        "workflows": {
            "test-workflow": {"description": "A test workflow", "path": "workflow.yaml"},
        }
    }
    with open(registry_dir / "index.yaml", "w") as f:
        yaml.dump(index_data, f)
    return registry_dir


def _configure_registry(registry_dir: Path, *, name: str = "my-reg") -> None:
    from conductor.registry.config import RegistryType, add_registry

    add_registry(name, str(registry_dir), registry_type=RegistryType.path, set_default=True)


# ---------------------------------------------------------------------------
# resolve_workflow (E12-T1)
# ---------------------------------------------------------------------------


class TestResolveWorkflowFile:
    def test_resolves_existing_file(self, workflow_file: Path) -> None:
        resolved = resolve_workflow(str(workflow_file))

        assert isinstance(resolved, ResolvedWorkflow)
        assert resolved.path == workflow_file
        assert resolved.name == "test-workflow"
        assert resolved.description == "A minimal workflow for launch tests"
        assert set(resolved.inputs) == {"question", "verbose", "retries"}
        assert resolved.inputs["question"].type == "string"
        assert resolved.inputs["question"].required is True
        assert resolved.inputs["verbose"].default is False

    def test_nonexistent_file_raises_launch_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.yaml"

        with pytest.raises(LaunchError, match="not found"):
            resolve_workflow(str(missing))

    def test_malformed_workflow_raises_launch_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("not: [valid, workflow")

        with pytest.raises(LaunchError, match="Failed to parse"):
            resolve_workflow(str(bad))


class TestResolveWorkflowRegistry:
    def test_resolves_registry_reference(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _registry_env(tmp_path, monkeypatch)
        _configure_registry(_write_registry(tmp_path), name="my-reg")

        resolved = resolve_workflow("test-workflow@my-reg")

        assert resolved.name == "test-workflow"
        assert resolved.path.name == "workflow.yaml"
        assert set(resolved.inputs) == {"question", "verbose", "retries"}

    def test_unknown_registry_raises_launch_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _registry_env(tmp_path, monkeypatch)

        with pytest.raises(LaunchError):
            resolve_workflow("some-workflow@no-such-registry")

    def test_unknown_workflow_in_registry_raises_launch_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _registry_env(tmp_path, monkeypatch)
        _configure_registry(_write_registry(tmp_path), name="my-reg")

        with pytest.raises(LaunchError):
            resolve_workflow("no-such-workflow@my-reg")


# ---------------------------------------------------------------------------
# coerce_input_value (E12-T3's type coercion requirement)
# ---------------------------------------------------------------------------


class TestCoerceInputValue:
    def test_string_passthrough(self) -> None:
        input_def = InputDef(type="string")
        assert coerce_input_value("hello", input_def) == "hello"

    @pytest.mark.parametrize("raw", ["true", "True", "1", "yes"])
    def test_boolean_true_variants(self, raw: str) -> None:
        input_def = InputDef(type="boolean")
        assert coerce_input_value(raw, input_def) is True

    @pytest.mark.parametrize("raw", ["false", "False", "0", "no"])
    def test_boolean_false_variants(self, raw: str) -> None:
        input_def = InputDef(type="boolean")
        assert coerce_input_value(raw, input_def) is False

    def test_boolean_invalid_raises_launch_error(self) -> None:
        input_def = InputDef(type="boolean")
        with pytest.raises(LaunchError, match="boolean"):
            coerce_input_value("maybe", input_def)

    def test_number_int(self) -> None:
        input_def = InputDef(type="number")
        assert coerce_input_value("42", input_def) == 42
        assert isinstance(coerce_input_value("42", input_def), int)

    def test_number_float(self) -> None:
        input_def = InputDef(type="number")
        assert coerce_input_value("3.14", input_def) == 3.14

    def test_number_invalid_raises_launch_error(self) -> None:
        input_def = InputDef(type="number")
        with pytest.raises(LaunchError, match="number"):
            coerce_input_value("not-a-number", input_def)

    def test_array_valid_json(self) -> None:
        input_def = InputDef(type="array")
        assert coerce_input_value("[1, 2, 3]", input_def) == [1, 2, 3]

    def test_array_invalid_json_raises_launch_error(self) -> None:
        input_def = InputDef(type="array")
        with pytest.raises(LaunchError, match="array"):
            coerce_input_value("not json", input_def)

    def test_array_wrong_json_shape_raises_launch_error(self) -> None:
        input_def = InputDef(type="array")
        with pytest.raises(LaunchError):
            coerce_input_value('{"a": 1}', input_def)

    def test_object_valid_json(self) -> None:
        input_def = InputDef(type="object")
        assert coerce_input_value('{"a": 1}', input_def) == {"a": 1}

    def test_object_wrong_json_shape_raises_launch_error(self) -> None:
        input_def = InputDef(type="object")
        with pytest.raises(LaunchError):
            coerce_input_value("[1, 2]", input_def)


# ---------------------------------------------------------------------------
# build_launch_inputs (required-field enforcement + defaults, E12-T3)
# ---------------------------------------------------------------------------


class TestBuildLaunchInputs:
    def test_coerces_all_provided_values(self) -> None:
        input_defs = {
            "question": InputDef(type="string", required=True),
            "verbose": InputDef(type="boolean", required=False, default=False),
        }
        raw_values = {"question": "What is Python?", "verbose": "true"}

        result = build_launch_inputs(raw_values, input_defs)

        assert result == {"question": "What is Python?", "verbose": True}

    def test_missing_required_field_raises_launch_error(self) -> None:
        input_defs = {"question": InputDef(type="string", required=True)}

        with pytest.raises(LaunchError, match="question"):
            build_launch_inputs({"question": ""}, input_defs)

    def test_missing_required_field_absent_key_raises_launch_error(self) -> None:
        input_defs = {"question": InputDef(type="string", required=True)}

        with pytest.raises(LaunchError, match="question"):
            build_launch_inputs({}, input_defs)

    def test_blank_optional_field_falls_back_to_default(self) -> None:
        input_defs = {"verbose": InputDef(type="boolean", required=False, default=True)}

        result = build_launch_inputs({"verbose": ""}, input_defs)

        assert result == {"verbose": True}

    def test_blank_optional_field_without_default_is_omitted(self) -> None:
        input_defs = {"note": InputDef(type="string", required=False)}

        result = build_launch_inputs({"note": ""}, input_defs)

        assert result == {}

    def test_bad_value_for_declared_type_raises_launch_error(self) -> None:
        input_defs = {"retries": InputDef(type="number", required=False, default=3)}

        with pytest.raises(LaunchError):
            build_launch_inputs({"retries": "not-a-number"}, input_defs)


# ---------------------------------------------------------------------------
# launch_workflow (E12-T2)
# ---------------------------------------------------------------------------


class TestLaunchWorkflow:
    def test_calls_launch_background_with_coerced_inputs(self, workflow_file: Path) -> None:
        input_defs = {
            "question": InputDef(type="string", required=True),
            "verbose": InputDef(type="boolean", required=False, default=False),
        }
        raw_values = {"question": "What is Python?", "verbose": "true"}

        fake_result = object()
        with patch("conductor.cli.bg_runner.launch_background", return_value=fake_result) as fake:
            result = launch_workflow(workflow_file, raw_values, input_defs)

        assert result is fake_result
        fake.assert_called_once()
        _args, kwargs = fake.call_args
        assert kwargs["workflow_path"] == workflow_file
        assert kwargs["inputs"] == {"question": "What is Python?", "verbose": True}

    def test_never_spawns_a_subprocess_directly(self, workflow_file: Path) -> None:
        """E12-T2: launch_workflow must delegate to launch_background() --
        it must never construct or spawn its own ``conductor`` subprocess
        (which would duplicate cli/bg_runner.py's detached-spawn logic)."""
        input_defs: dict[str, InputDef] = {}
        with (
            patch("conductor.cli.bg_runner.launch_background") as fake_launch,
            patch("subprocess.Popen") as fake_popen,
        ):
            launch_workflow(workflow_file, {}, input_defs)

        fake_launch.assert_called_once()
        fake_popen.assert_not_called()

    def test_forwards_optional_kwargs(self, workflow_file: Path) -> None:
        input_defs: dict[str, InputDef] = {}
        with patch("conductor.cli.bg_runner.launch_background") as fake_launch:
            launch_workflow(
                workflow_file,
                {},
                input_defs,
                provider_override="claude",
                skip_gates=True,
                metadata={"source": "fleet-tui"},
            )

        _args, kwargs = fake_launch.call_args
        assert kwargs["provider_override"] == "claude"
        assert kwargs["skip_gates"] is True
        assert kwargs["metadata"] == {"source": "fleet-tui"}

    def test_required_field_rejected_before_launch_background_is_called(
        self, workflow_file: Path
    ) -> None:
        input_defs = {"question": InputDef(type="string", required=True)}
        with (
            patch("conductor.cli.bg_runner.launch_background") as fake_launch,
            pytest.raises(LaunchError, match="question"),
        ):
            launch_workflow(workflow_file, {}, input_defs)

        fake_launch.assert_not_called()

    def test_launch_background_failure_surfaces_as_launch_error(self, workflow_file: Path) -> None:
        """Any launch_background() failure -- a child dying early, the
        dashboard never starting, or (D2) the child never writing a
        matching run record within the timeout -- must surface as a
        LaunchError with the original message, not propagate the raw
        RuntimeError/traceback."""
        with (
            patch(
                "conductor.cli.bg_runner.launch_background",
                side_effect=RuntimeError(
                    "Background process did not report a run record within 15 seconds "
                    "(run_id=abc123). The background process was terminated."
                ),
            ),
            pytest.raises(LaunchError, match="run record within 15 seconds"),
        ):
            launch_workflow(workflow_file, {}, {})


# ---------------------------------------------------------------------------
# Type-preserving transport across the launch_background CLI boundary
# ---------------------------------------------------------------------------
#
# ``build_launch_inputs`` coerces raw form strings to each InputDef's
# declared type, but that typed value must still survive the round trip
# through ``launch_background``'s ``--input`` CLI argument and back through
# the child's own ``cli/run.py::coerce_value`` parsing. A declared
# "string"-typed value like "true"/"42"/"null"/"[1]" must come back out as
# that same string, not be reinterpreted as bool/int/None/list.


class TestLaunchBoundaryTypePreservation:
    @pytest.mark.parametrize(
        "raw_value",
        ["true", "false", "42", "3.14", "null", "[1]", '{"a": 1}'],
    )
    def test_string_typed_value_round_trips_through_the_cli_boundary(self, raw_value: str) -> None:
        """A declared ``string`` input whose value happens to look like
        another type must still be a plain string after being serialized
        for ``launch_background``'s hidden, strictly-typed ``--input-json``
        flag and re-parsed by the child's ``coerce_typed_value`` -- not
        silently reinterpreted. (The public ``--input``/``coerce_value``
        heuristic is untouched and not part of this transport -- see
        ``test_cli/test_run.py::TestCoerceValue`` for its unchanged
        contract.)"""
        from conductor.cli.bg_runner import _serialize_input_value
        from conductor.cli.run import coerce_typed_value

        input_def = InputDef(type="string", required=True)
        coerced = coerce_input_value(raw_value, input_def)
        assert coerced == raw_value

        serialized = _serialize_input_value(coerced)
        round_tripped = coerce_typed_value(serialized)

        assert round_tripped == raw_value
        assert isinstance(round_tripped, str)

    @pytest.mark.parametrize(
        ("input_def", "raw_value", "expected"),
        [
            (InputDef(type="boolean", required=True), "true", True),
            (InputDef(type="number", required=True), "42", 42),
            (InputDef(type="number", required=True), "3.14", 3.14),
            (InputDef(type="array", required=True), "[1, 2]", [1, 2]),
            (InputDef(type="object", required=True), '{"a": 1}', {"a": 1}),
        ],
    )
    def test_non_string_typed_value_round_trips_through_the_cli_boundary(
        self, input_def: InputDef, raw_value: str, expected: object
    ) -> None:
        """Non-string declared types must also survive the boundary
        unchanged -- the fix must not regress the already-correct cases."""
        from conductor.cli.bg_runner import _serialize_input_value
        from conductor.cli.run import coerce_typed_value

        coerced = coerce_input_value(raw_value, input_def)
        serialized = _serialize_input_value(coerced)
        round_tripped = coerce_typed_value(serialized)

        assert round_tripped == expected
        assert type(round_tripped) is type(expected)
