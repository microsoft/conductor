"""Unit tests for TemplateRenderer with FileString templates.

These tests verify that TemplateRenderer correctly loads templates using FileSystemLoader
when a FileString is provided, supporting includes, imports, and inheritance,
while maintaining feature parity (custom filters, StrictUndefined, dict-safe attributes).
"""

from pathlib import Path, PureWindowsPath

import pytest

from conductor.exceptions import TemplateError
from conductor.executor.template import TemplateRenderer
from conductor.file_string import FileString


def test_render_file_string_with_include(tmp_path: Path) -> None:
    """Requirement: tmp_path with main.md containing {% include "_partial.md" %} and _partial.md.

    Render FileString(main_content, main_path) -> includes partial content;
    context vars render in both.
    """
    main_file = tmp_path / "main.md"
    partial_file = tmp_path / "_partial.md"

    main_file.write_text("Hello {{ name }}! {% include '_partial.md' %}")
    partial_file.write_text("This is a partial template for {{ target }}.")

    file_string = FileString(main_file.read_text(), main_file)
    renderer = TemplateRenderer()

    result = renderer.render(file_string, {"name": "Alice", "target": "Bob"})
    assert result == "Hello Alice! This is a partial template for Bob."


def test_render_file_string_with_import(tmp_path: Path) -> None:
    """Requirement: import macro from another file.

    Template contains {% import "_macros.md" as m %}{{ m.greet(name) }}.
    """
    main_file = tmp_path / "main.md"
    macros_file = tmp_path / "_macros.md"

    main_file.write_text("{% import '_macros.md' as m %}{{ m.greet(name) }}")
    macros_file.write_text("{% macro greet(val) %}Greetings, {{ val }}!{% endmacro %}")

    file_string = FileString(main_file.read_text(), main_file)
    renderer = TemplateRenderer()

    result = renderer.render(file_string, {"name": "Charlie"})
    assert result == "Greetings, Charlie!"


def test_render_file_string_with_extends(tmp_path: Path) -> None:
    """Requirement: extend a base template.

    Template contains {% extends "_base.md" %}{% block body %}...
    with _base.md declaring {% block body %}{% endblock %}.
    """
    main_file = tmp_path / "main.md"
    base_file = tmp_path / "_base.md"

    main_file.write_text("{% extends '_base.md' %}{% block body %}Child Content{% endblock %}")
    base_file.write_text("Base Header | {% block body %}{% endblock %} | Base Footer")

    file_string = FileString(main_file.read_text(), main_file)
    renderer = TemplateRenderer()

    result = renderer.render(file_string, {})
    assert result == "Base Header | Child Content | Base Footer"


def test_render_system_prompt_file_string_with_include(tmp_path: Path) -> None:
    """Requirement: same include mechanics for a system prompt simulation.

    This ensures that prompt vs system_prompt is not special-cased and both
    rely on the underlying FileString include support.
    """
    sys_file = tmp_path / "system.md"
    partial_file = tmp_path / "_partial.md"

    sys_file.write_text("System instructions: {% include '_partial.md' %}")
    partial_file.write_text("Be concise.")

    file_string = FileString(sys_file.read_text(), sys_file)
    renderer = TemplateRenderer()

    result = renderer.render(file_string, {})
    assert result == "System instructions: Be concise."


def test_inline_prompt_with_include_still_fails() -> None:
    """Requirement: plain str with {% include %} raises TemplateError.

    Ensures that default inline template rendering does not use FileSystemLoader
    and throws TemplateError when attempting to load another template.
    """
    renderer = TemplateRenderer()
    with pytest.raises(TemplateError):
        renderer.render("Hello {% include '_partial.md' %}", {})


def test_file_string_custom_filters_available(tmp_path: Path) -> None:
    """Requirement: json and default filters work in the FileSystemLoader branch.

    e.g. {{ items | json }} and {{ missing | default("x") }} semantics.
    Using defined None value for default filter because of StrictUndefined.
    """
    main_file = tmp_path / "main.md"
    main_file.write_text("JSON: {{ items | json }}, Default: {{ missing | default('fallback') }}")

    file_string = FileString(main_file.read_text(), main_file)
    renderer = TemplateRenderer()

    result = renderer.render(file_string, {"items": ["a", "b"], "missing": None})
    assert 'JSON: [\n  "a",\n  "b"\n]' in result
    assert "Default: fallback" in result


def test_file_string_strict_undefined_still_enforced(tmp_path: Path) -> None:
    """Requirement: missing variable in FileString template raises TemplateError.

    Maintains parity with the inline StrictUndefined rendering.
    """
    main_file = tmp_path / "main.md"
    main_file.write_text("Hello {{ undefined_var }}!")

    file_string = FileString(main_file.read_text(), main_file)
    renderer = TemplateRenderer()

    with pytest.raises(TemplateError) as exc_info:
        renderer.render(file_string, {})
    assert "undefined_var" in str(exc_info.value).lower()


def test_file_string_dict_safe_getattr(tmp_path: Path) -> None:
    """Requirement: _DictSafeEnvironment behavior preserved in file branch.

    Ensures dict keys named like dict methods (e.g., "items") are accessed correctly
    rather than returning the method itself.
    """
    main_file = tmp_path / "main.md"
    main_file.write_text("Items count: {{ obj.items }}")

    file_string = FileString(main_file.read_text(), main_file)
    renderer = TemplateRenderer()

    result = renderer.render(file_string, {"obj": {"items": 42}})
    assert result == "Items count: 42"


def test_file_string_windows_path_conversion(tmp_path: Path) -> None:
    """Requirement: test that constructs FileString with a PureWindowsPath-style string
    converted via Path; keep it platform-safe so it also passes on Linux.
    """
    # 1. Non-existent Windows path - should fall back to inline render safely
    win_path_str = "C:\\foo\\bar\\main.md"
    path_obj = Path(PureWindowsPath(win_path_str))

    file_string = FileString("Hello {{ name }}!", path_obj)
    renderer = TemplateRenderer()
    result = renderer.render(file_string, {"name": "World"})
    assert result == "Hello World!"

    # 2. Existing path converted via PureWindowsPath to ensure FileSystemLoader works
    main_file = tmp_path / "main.md"
    partial_file = tmp_path / "_partial.md"
    main_file.write_text("Hello {% include '_partial.md' %}!")
    partial_file.write_text("World")

    win_path = PureWindowsPath(main_file)
    path_obj_existing = Path(win_path)

    file_string_existing = FileString(main_file.read_text(), path_obj_existing)
    result_existing = renderer.render(file_string_existing, {})
    assert result_existing == "Hello World!"
