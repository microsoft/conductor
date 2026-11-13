"""Managed directory translation uses only declared provenance and the live tree."""

from __future__ import annotations

import os
import traceback
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from conductor.engine.local_workspace import (
    LocalWorkspacePathError,
    LocalWorkspacePathMapper,
    _logical_directory,
)
from conductor.execution.types import (
    BundleRef,
    LocalWorkspaceBinding,
    ManagedWorkspaceContext,
    ProcessScopeController,
)
from conductor.executor.template import TemplateRenderer


@pytest.fixture
def binding(tmp_path: Path) -> LocalWorkspaceBinding:
    source = tmp_path / "source"
    source.mkdir()
    work = tmp_path / "workspace/work"
    (work / "main").mkdir(parents=True)
    bundle = BundleRef(
        "sha256:test",
        str(tmp_path / "absent-cas"),
        source_roots=((str(source), "main"),),
    )
    return LocalWorkspaceBinding(
        "run",
        "incarnation",
        str(work.parent),
        str(work),
        bundle,
        ManagedWorkspaceContext(str(work.parent), "incarnation", Mock(spec=ProcessScopeController)),
    )


@pytest.mark.parametrize("rendered", [None, ".", "new", "nested/../new"])
def test_live_created_cwd(binding: LocalWorkspaceBinding, rendered: str | None) -> None:
    # Requirement: default and relative cwd use live work, not source or CAS existence.
    work = Path(binding.work_root)
    (work / "main/new").mkdir()
    expected = work / ("main" if rendered in (None, ".") else "main/new")
    assert LocalWorkspacePathMapper(binding)(rendered) == str(expected)
    assert not (Path(binding.bundle.source_roots[0][0]) / "new").exists()
    assert not Path(binding.bundle.store_path).exists()


def test_absolute_live_child(binding: LocalWorkspaceBinding) -> None:
    # Requirement: a host path created only in work still maps through its declared root.
    (Path(binding.work_root) / "main/generated").mkdir()
    source = Path(binding.bundle.source_roots[0][0])
    assert LocalWorkspacePathMapper(binding)(str(source / "generated")) == str(
        Path(binding.work_root) / "main/generated"
    )


# Requirement: Jinja expression uses Windows-valid chars (no wildcard '*') evaluating to 42.
@pytest.mark.parametrize("value", ["new", "{{ 6 + 36 }}"])
def test_render_once_boundary(binding: LocalWorkspaceBinding, value: str) -> None:
    # Requirement: executors render once; a resulting template expression remains path data.
    main = Path(binding.work_root) / "main"
    (main / "new").mkdir()
    (main / "42").mkdir()
    rendered = TemplateRenderer().render("{{ path }}", {"path": value})
    if value == "new":
        assert LocalWorkspacePathMapper(binding)(rendered) == str(main / "new")
    else:
        with pytest.raises(LocalWorkspacePathError) as caught:
            LocalWorkspacePathMapper(binding)(rendered)
        assert caught.value.reason == "unavailable_directory"


def test_source_sentinel_unchanged(binding: LocalWorkspaceBinding) -> None:
    # Requirement: translation never mutates source bytes or inode and never changes process cwd.
    source = Path(binding.bundle.source_roots[0][0]) / "sentinel"
    source.write_bytes(b"source-sentinel")
    before = source.stat()
    cwd = Path.cwd()
    assert LocalWorkspacePathMapper(binding)(None) == str(Path(binding.work_root) / "main")
    assert (source.stat().st_dev, source.stat().st_ino) == (before.st_dev, before.st_ino)
    assert source.read_bytes() == b"source-sentinel"
    assert Path.cwd() == cwd


def test_regular_file_refused(binding: LocalWorkspaceBinding) -> None:
    # Requirement: a staged regular file cannot be used as a directory.
    (Path(binding.work_root) / "main/file").write_bytes(b"not a directory")
    with pytest.raises(LocalWorkspacePathError) as caught:
        LocalWorkspacePathMapper(binding)("file")
    assert caught.value.reason == "not_directory"


def test_longest_root_and_agent_paths(binding: LocalWorkspaceBinding) -> None:
    # Requirement: containing roots compete by components, including explicit agent paths.
    source = Path(binding.bundle.source_roots[0][0])
    work = Path(binding.work_root)
    (work / "roots/nested/sub").mkdir(parents=True)
    (work / "skills/task").mkdir(parents=True)
    bundle = replace(
        binding.bundle,
        source_roots=(*binding.bundle.source_roots, (str(source / "nested"), "roots/nested")),
        agent_paths=((str(source / "skill"), "skills/task"),),
    )
    mapper = LocalWorkspacePathMapper(replace(binding, bundle=bundle))
    assert mapper(str(source / "nested/sub")) == str(work / "roots/nested/sub")
    assert mapper.map_directory(str(source / "skill")) == str(work / "skills/task")


def test_child_namespace(binding: LocalWorkspaceBinding) -> None:
    # Requirement: child defaults are rooted at the current workflow, not root main.
    child = Path(binding.work_root) / "registry/child/sub"
    child.mkdir(parents=True)
    mapper = LocalWorkspacePathMapper(binding, "registry/child")
    assert mapper(None) == str(child.parent)
    assert mapper("sub") == str(child)


@pytest.mark.parametrize(
    ("rendered", "reason"),
    [
        ("", "invalid_path"),
        (" \t", "invalid_path"),
        ("secret\0value", "invalid_path"),
        ("../outside", "namespace_escape"),
        ("nested/../../outside", "namespace_escape"),
        ("absent", "unavailable_directory"),
        ("{{ secret }}", "unavailable_directory"),
        ("{% include 'secret' %}", "unavailable_directory"),
    ],
)
def test_refusal_before_dispatch(
    binding: LocalWorkspaceBinding, rendered: str, reason: str
) -> None:
    # Requirement: malformed, escaped, missing and second-render paths fail before dispatch.
    dispatched = False
    with pytest.raises(LocalWorkspacePathError) as caught:
        LocalWorkspacePathMapper(binding)(rendered)
        dispatched = True
    assert caught.value.reason == reason
    assert not dispatched


def test_unknown_absolute_and_prefix_sibling(binding: LocalWorkspaceBinding) -> None:
    # Requirement: neither host existence nor a string prefix authorizes a directory.
    source = Path(binding.bundle.source_roots[0][0])
    sibling = source.with_name(source.name + "-other")
    sibling.mkdir()
    with pytest.raises(LocalWorkspacePathError) as caught:
        LocalWorkspacePathMapper(binding)(str(sibling))
    assert caught.value.reason == "undeclared_absolute_path"


def test_symlink_escape(binding: LocalWorkspaceBinding, tmp_path: Path) -> None:
    # Requirement: an outward symlink is refused after lexical translation.
    link = Path(binding.work_root) / "main/link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(LocalWorkspacePathError) as caught:
        LocalWorkspacePathMapper(binding)("link")
    assert caught.value.reason == "symlink_escape"


def test_internal_symlink(binding: LocalWorkspaceBinding) -> None:
    # Requirement: an in-tree link may name a live directory.
    main = Path(binding.work_root) / "main"
    (main / "target").mkdir()
    (main / "link").symlink_to("target", target_is_directory=True)
    assert LocalWorkspacePathMapper(binding)("link") == str(main / "target")


def test_live_state_is_not_cached(binding: LocalWorkspaceBinding) -> None:
    # Requirement: replacing a previously valid directory with an escape invalidates mapping.
    main = Path(binding.work_root) / "main"
    (main / "live").mkdir()
    mapper = LocalWorkspacePathMapper(binding)
    assert mapper("live") == str(main / "live")
    (main / "live").rmdir()
    (main / "live").symlink_to(main.parent.parent, target_is_directory=True)
    with pytest.raises(LocalWorkspacePathError) as caught:
        mapper("live")
    assert caught.value.reason == "symlink_escape"


def test_path_errors_hide_secret_values(binding: LocalWorkspaceBinding) -> None:
    # Requirement: formatted exceptions and chained OS errors never disclose rendered paths.
    secret = "private-token-817"
    with pytest.raises(LocalWorkspacePathError) as caught:
        LocalWorkspacePathMapper(binding)(secret)
    assert secret not in "".join(traceback.format_exception(caught.value))
    assert secret not in repr(caught.value)
    assert caught.value.__suppress_context__


def test_selected_scope_is_not_union_grant(binding: LocalWorkspaceBinding) -> None:
    # Requirement: narrow skill owners use frozen selection instead of flat full-plugin aliases.
    work = Path(binding.work_root)
    narrow = "skills/task/plugin"
    full = "plugins/owner"
    (work / narrow / "skills/task").mkdir(parents=True)
    (work / full).mkdir(parents=True)
    bundle = replace(binding.bundle, agent_paths=(("/host/owner", full),))
    mapper = LocalWorkspacePathMapper(replace(binding, bundle=bundle))
    assert mapper.map_selected_directory(narrow) == str(work / narrow)
    assert mapper.map_selected_directory(narrow + "/skills/task") == str(
        work / narrow / "skills/task"
    )


@pytest.mark.parametrize("namespace", ["", ".", "../main", "/main", "tree/../main", r"main\sub"])
def test_invalid_selected_namespace(binding: LocalWorkspaceBinding, namespace: str) -> None:
    # Requirement: selected dependency directories are full-tree relative without traversal.
    with pytest.raises(LocalWorkspacePathError) as caught:
        LocalWorkspacePathMapper(binding).map_selected_directory(namespace)
    assert caught.value.reason == "invalid_namespace"


@pytest.mark.parametrize(
    ("value", "root", "expected"),
    [
        (r"C:\Repo\new", r"c:\repo", "main/new"),
        (r"C:/REPO/new", r"c:\repo", "main/new"),
        (r"\\Server\Share\Repo\new", r"\\server\share\repo", "main/new"),
        (r"sub\new", r"C:\repo", "main/sub/new"),
    ],
)
def test_windows_components(value: str, root: str, expected: str) -> None:
    # Requirement: Windows drive, UNC, separators and case use Windows lexical semantics.
    assert _logical_directory(value, ((root, "main"),), "main", windows=True).as_posix() == (
        expected
    )


@pytest.mark.parametrize(
    "value",
    [
        r"D:\repo",
        r"C:\repo-other",
        r"C:relative",
        r"\repo",
        r"\\server\other\repo",
        r"\\?\C:\repo",
        r"C:\repo\NUL",
        r"C:\repo\bad.",
        r"C:\repo\bad ",
        r"C:\repo\file:stream",
        r"..\outside",
    ],
)
def test_windows_invalid_paths(value: str) -> None:
    # Requirement: drive mismatch, rooted-relative paths, device aliases and escapes fail closed.
    with pytest.raises(LocalWorkspacePathError):
        _logical_directory(value, ((r"C:\repo", "main"),), "main", windows=True)


def test_windows_root_case_collision() -> None:
    # Requirement: case aliases with conflicting namespaces cannot silently pick a winner.
    with pytest.raises(LocalWorkspacePathError) as caught:
        _logical_directory(
            r"C:\repo",
            ((r"C:\Repo", "main"), (r"c:\repo", "other")),
            "main",
            windows=True,
        )
    assert caught.value.reason == "source_root_collision"


def test_posix_case_sensitive() -> None:
    # Requirement: POSIX roots remain case-sensitive and use component containment.
    with pytest.raises(LocalWorkspacePathError):
        _logical_directory("/Repo/sub", (("/repo", "main"),), "main", windows=False)


@pytest.mark.skipif(os.name == "nt", reason="Braces are literal POSIX path components")
def test_prompt_looking_path_is_data(binding: LocalWorkspaceBinding) -> None:
    # Requirement: a literal template-looking directory is not evaluated a second time.
    literal = "{{ 6 * 7 }}"
    target = Path(binding.work_root) / "main" / literal
    target.mkdir()
    assert LocalWorkspacePathMapper(binding)(literal) == str(target)
