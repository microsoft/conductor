"""Unit tests for the shared ``is_jinja_template`` helper (#263 review)."""

from __future__ import annotations

import pytest

from conductor.templating import is_jinja_template


@pytest.mark.parametrize(
    "value",
    [
        "{{ workflow.input.eff }}",
        "{% if x %}high{% endif %}",
        "prefix {{ x }} suffix",
        "{%- trim -%}",
    ],
)
def test_detects_expression_and_statement_templates(value: str) -> None:
    assert is_jinja_template(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "high",
        "long_context",
        "",
        "{ not a template }",
        "100",
    ],
)
def test_rejects_plain_strings(value: str) -> None:
    assert is_jinja_template(value) is False


@pytest.mark.parametrize("value", [None, 123, ["{{ x }}"], {"k": "{{ x }}"}])
def test_rejects_non_strings(value: object) -> None:
    # ``None`` in particular must be False so the ``model`` render guard can drop
    # its old ``agent.model and ...`` short-circuit.
    assert is_jinja_template(value) is False
