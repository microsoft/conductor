"""Unit tests for :mod:`conductor.executor.set_step`.

Covers:
- Auto type detection for scalar/list/dict/bool/int/float and string fallback
- Empty-string handling (returns ``""``, not ``None``)
- Each explicit ``output_type`` branch (success + failure)
- Multi-binding rendering of all keys
- JSON-safety normalisation (datetime/date → ISO; rejection of non-JSON types)
- Template error propagation
"""

from __future__ import annotations

import datetime as _dt

import pytest

from conductor.config.schema import AgentDef
from conductor.exceptions import ExecutionError, TemplateError
from conductor.executor.set_step import (
    SET_VALUE_REPR_MAX,
    SetExecutor,
    _coerce,
    _to_json_safe,
    render_set_value_repr,
)


@pytest.fixture
def executor() -> SetExecutor:
    return SetExecutor()


class TestSetExecutorSingleValue:
    """Single ``value:`` step coverage."""

    def test_string_auto(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="{{ a }}/{{ b }}")
        out = executor.execute(agent, {"a": "myorg", "b": "myrepo"})
        assert out.value == "myorg/myrepo"
        assert out.is_multi is False
        assert out.output_type == "auto"

    def test_integer_auto(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="{{ n + 1 }}")
        out = executor.execute(agent, {"n": 41})
        assert out.value == 42
        assert isinstance(out.value, int)

    def test_float_auto(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="{{ 3.14 }}")
        out = executor.execute(agent, {})
        assert out.value == 3.14

    def test_boolean_auto(self, executor: SetExecutor) -> None:
        agent = AgentDef(
            name="x",
            type="set",
            value="{{ severity in ['high', 'critical'] }}",
        )
        out = executor.execute(agent, {"severity": "high"})
        assert out.value is True

    def test_list_auto(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="{{ [1, 2, 3] }}")
        out = executor.execute(agent, {})
        assert out.value == [1, 2, 3]

    def test_dict_auto(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="{{ {'a': 1} }}")
        out = executor.execute(agent, {})
        assert out.value == {"a": 1}

    def test_empty_string_becomes_empty_string_not_none(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="")
        out = executor.execute(agent, {})
        assert out.value == ""

    def test_whitespace_only_becomes_empty_string(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="   \n  ")
        out = executor.execute(agent, {})
        assert out.value == ""

    def test_explicit_null_keyword_returns_none(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="null")
        out = executor.execute(agent, {})
        assert out.value is None

    def test_empty_render_via_template_returns_empty_string(self, executor: SetExecutor) -> None:
        """Template rendering to empty hits the not-stripped short-circuit."""
        agent = AgentDef(name="x", type="set", value="{{ '' }}")
        out = executor.execute(agent, {})
        assert out.value == ""

    def test_date_like_render_normalised_to_iso(self, executor: SetExecutor) -> None:
        """End-to-end auto detection: YAML date → ISO 8601 string after
        ``_to_json_safe`` runs."""
        agent = AgentDef(name="x", type="set", value="2024-01-02")
        out = executor.execute(agent, {})
        assert out.value == "2024-01-02"
        assert isinstance(out.value, str)


class TestSetExecutorMultiValues:
    """Multi ``values:`` step coverage."""

    def test_multi_binds_each_key(self, executor: SetExecutor) -> None:
        agent = AgentDef(
            name="d",
            type="set",
            values={
                "is_breaking": "{{ severity in ['high', 'critical'] }}",
                "target_branch": "{{ branch or 'main' }}",
                "model": "claude-{{ ver }}",
            },
        )
        out = executor.execute(agent, {"severity": "high", "branch": None, "ver": "sonnet-4-5"})
        assert out.is_multi is True
        assert out.value == {
            "is_breaking": True,
            "target_branch": "main",
            "model": "claude-sonnet-4-5",
        }

    def test_multi_does_not_see_earlier_bindings(self, executor: SetExecutor) -> None:
        """Per the issue's resolution, multi-value steps don't see prior
        bindings within the same step. The second binding reads ``a`` from the
        *original* context (not the rendered ``first`` produced earlier in the
        same step)."""
        agent = AgentDef(
            name="d",
            type="set",
            values={
                "first": "{{ a }}-modified",
                "second": "{{ a }}",
            },
        )
        out = executor.execute(agent, {"a": "hello"})
        assert out.value["first"] == "hello-modified"
        # Original `a` is used here — not the rendered `first` from this step.
        assert out.value["second"] == "hello"

    def test_date_like_render_in_multi_normalised_to_iso(self, executor: SetExecutor) -> None:
        """Per-binding ``_to_json_safe`` also runs for multi-values steps."""
        agent = AgentDef(
            name="x",
            type="set",
            values={"d": "2024-01-02", "t": "12:30:45"},
        )
        out = executor.execute(agent, {})
        assert out.value == {"d": "2024-01-02", "t": "12:30:45"}
        assert isinstance(out.value["d"], str)
        assert isinstance(out.value["t"], str)


class TestSetExecutorExplicitOutputType:
    """Explicit ``output_type:`` overrides on single ``value:`` only."""

    def test_string_keeps_raw(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="1.2.3", output_type="string")
        out = executor.execute(agent, {})
        assert out.value == "1.2.3"
        assert isinstance(out.value, str)

    def test_integer_success(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="42", output_type="integer")
        out = executor.execute(agent, {})
        assert out.value == 42

    def test_integer_failure(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="not-a-number", output_type="integer")
        with pytest.raises(ExecutionError, match="to integer"):
            executor.execute(agent, {})

    def test_number_int(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="42", output_type="number")
        out = executor.execute(agent, {})
        assert out.value == 42

    def test_number_float(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="3.14", output_type="number")
        out = executor.execute(agent, {})
        assert out.value == 3.14

    def test_number_failure(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="not-a-number", output_type="number")
        with pytest.raises(ExecutionError, match="to number"):
            executor.execute(agent, {})

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("false", False),
            ("False", False),
            ("1", True),
            ("0", False),
            ("yes", True),
            ("no", False),
            ("y", True),
            ("n", False),
            ("on", True),
            ("off", False),
        ],
    )
    def test_boolean_success(self, executor: SetExecutor, text: str, expected: bool) -> None:
        agent = AgentDef(name="x", type="set", value=text, output_type="boolean")
        out = executor.execute(agent, {})
        assert out.value is expected

    def test_boolean_failure(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="maybe", output_type="boolean")
        with pytest.raises(ExecutionError, match="to boolean"):
            executor.execute(agent, {})

    def test_list_success(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="[1, 2, 3]", output_type="list")
        out = executor.execute(agent, {})
        assert out.value == [1, 2, 3]

    def test_list_failure_on_dict(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="{a: 1}", output_type="list")
        with pytest.raises(ExecutionError, match="output_type: list"):
            executor.execute(agent, {})

    def test_dict_success(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="{a: 1, b: 2}", output_type="dict")
        out = executor.execute(agent, {})
        assert out.value == {"a": 1, "b": 2}

    def test_dict_failure_on_scalar(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="42", output_type="dict")
        with pytest.raises(ExecutionError, match="output_type: dict"):
            executor.execute(agent, {})


class TestSetExecutorTemplateErrors:
    """Template rendering failures propagate as TemplateError."""

    def test_undefined_variable_raises(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", value="{{ does_not_exist }}")
        with pytest.raises(TemplateError):
            executor.execute(agent, {})

    def test_undefined_in_multi_raises(self, executor: SetExecutor) -> None:
        agent = AgentDef(name="x", type="set", values={"a": "{{ ok }}", "b": "{{ missing }}"})
        with pytest.raises(TemplateError):
            executor.execute(agent, {"ok": "hi"})


class TestJsonSafeNormalisation:
    """``_to_json_safe`` normalisation rules."""

    def test_scalars_pass_through(self) -> None:
        for v in (None, True, False, 0, 1, 3.14, "hello"):
            assert _to_json_safe(v, "label") == v

    def test_datetime_to_iso(self) -> None:
        d = _dt.date(2024, 1, 2)
        assert _to_json_safe(d, "label") == "2024-01-02"
        dt = _dt.datetime(2024, 1, 2, 12, 30, 45)
        assert _to_json_safe(dt, "label") == "2024-01-02T12:30:45"
        t = _dt.time(12, 30, 45)
        assert _to_json_safe(t, "label") == "12:30:45"

    def test_tuple_becomes_list(self) -> None:
        assert _to_json_safe((1, 2, 3), "label") == [1, 2, 3]

    def test_nested_containers(self) -> None:
        v = {"a": [1, _dt.date(2024, 1, 2), {"b": _dt.time(12, 0)}]}
        assert _to_json_safe(v, "label") == {"a": [1, "2024-01-02", {"b": "12:00:00"}]}

    def test_non_string_dict_key_raises(self) -> None:
        """Non-string dict keys raise instead of being silently coerced.

        Silent coercion would risk collisions like ``{1: "a", "1": "b"}`` →
        one entry lost; raising keeps the JSON-safe contract honest.
        """
        with pytest.raises(ExecutionError, match="not JSON-safe"):
            _to_json_safe({1: "x", 2: "y"}, "label")

    def test_unknown_type_raises(self) -> None:
        class Custom:
            pass

        with pytest.raises(ExecutionError, match="not JSON-safe"):
            _to_json_safe(Custom(), "label")


class TestRenderSetValueRepr:
    """`render_set_value_repr` is shared by the engine emitter and the web
    server's synthetic-replay branch — keep its truncation contract pinned."""

    def test_short_scalar_unchanged(self) -> None:
        assert render_set_value_repr("hello") == '"hello"'

    def test_short_dict_serialised(self) -> None:
        assert render_set_value_repr({"a": 1, "b": True}) == '{"a": 1, "b": true}'

    def test_long_value_truncated_with_marker(self) -> None:
        big = "x" * (SET_VALUE_REPR_MAX * 2)
        result = render_set_value_repr(big)
        assert result.endswith("… [truncated]")
        # The truncated portion is exactly SET_VALUE_REPR_MAX chars of the
        # JSON-serialised string (which includes the leading quote).
        assert len(result) == SET_VALUE_REPR_MAX + len("… [truncated]")

    def test_short_value_no_marker(self) -> None:
        short = "x" * (SET_VALUE_REPR_MAX - 10)
        result = render_set_value_repr(short)
        assert "[truncated]" not in result


class TestCoerceDirect:
    """Direct ``_coerce`` coverage for branches not exercised through execute()."""

    def test_auto_yaml_parse_failure_falls_back_to_raw_string(self) -> None:
        """Malformed YAML falls back to the raw rendered string verbatim."""
        # Unclosed flow mapping — YAML parse error, not recoverable.
        raw = "{a: 1, b:"
        result = _coerce(raw, "auto", "label")
        assert result == raw

    def test_auto_pure_comment_render_kept_as_string(self) -> None:
        """yaml.safe_load returns None for pure-comment renders; the auto
        branch should keep the raw string so the user doesn't get a surprise
        null bind."""
        raw = "# just a comment"
        result = _coerce(raw, "auto", "label")
        assert result == raw

    def test_auto_explicit_null_marker_returns_none(self) -> None:
        for marker in ("null", "~", "Null", "NULL"):
            assert _coerce(marker, "auto", "label") is None

    def test_date_like_yaml_string_normalised_after_coerce(self) -> None:
        """A date-like string parses to a ``date`` in YAML, then JSON-safe
        normalisation in the executor would convert it back to ISO."""
        # _coerce alone returns the date; _to_json_safe handles normalisation.
        parsed = _coerce("2024-01-02", "auto", "label")
        assert isinstance(parsed, _dt.date)
