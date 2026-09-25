"""Tests for the loader include-provenance recorder and ``load_config_with_graph``.

These tests cover the include-provenance graph added on top of the existing
loader semantics: the root workflow file is recorded first, every
``!file``/``!yamlfile`` include is appended in read order (never
deduplicated), digests are computed over the raw bytes before ``${VAR}``
expansion, and the plain ``load_config`` path performs zero additional file
I/O compared to the pre-change implementation.
"""

from __future__ import annotations

import builtins
import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.config.loader import (
    ConfigLoader,
    IncludedFile,
    IncludedFilesGraph,
    load_config,
    load_config_with_graph,
)
from conductor.exceptions import ConfigurationError

_WORKFLOW_TEMPLATE = """\
workflow:
  name: graph-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: !file {prompt_ref}
    routes:
      - to: $end
"""


def _sha256(raw: bytes) -> str:
    """Compute the expected ``sha256:<hex>`` digest spelling."""
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _write_workflow(tmp_path: Path, prompt_ref: str = "prompts/review.md") -> Path:
    """Write a minimal workflow that includes one prompt file via ``!file``."""
    (tmp_path / "prompts").mkdir(exist_ok=True)
    workflow_path = tmp_path / "workflow.yaml"
    workflow_path.write_text(_WORKFLOW_TEMPLATE.format(prompt_ref=prompt_ref))
    return workflow_path


class TestSingleFileInclude:
    """Provenance recording for a single ``!file`` include."""

    def test_single_file_include_recorded(self, tmp_path: Path) -> None:
        # Requirement: a single !file include is recorded with its raw
        # logical_ref, a correct parent edge to the resolved root file, and a
        # ``sha256:`` digest of the file's raw bytes.
        raw = b"You are a helpful assistant.\n"
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "review.md").write_bytes(raw)
        workflow_path = _write_workflow(tmp_path)

        config, graph = load_config_with_graph(workflow_path)

        assert isinstance(graph, IncludedFilesGraph)
        assert len(graph) == 2
        root, include = graph.files

        # Root recorded as node zero.
        assert root.tag == "workflow"
        assert root.parent is None
        assert root.path == workflow_path.resolve()
        assert root.logical_ref == str(workflow_path)

        assert include.tag == "file"
        assert include.logical_ref == "prompts/review.md"
        assert include.path == (tmp_path / "prompts" / "review.md").resolve()
        assert include.parent == workflow_path.resolve()
        assert include.digest == _sha256(raw)
        assert include.size == len(raw)

        # The loaded config still sees the content.
        assert "helpful assistant" in config.agents[0].prompt

    def test_digest_computed_before_env_expansion(self, tmp_path: Path) -> None:
        # Requirement: for a file containing a ${VAR} placeholder, the
        # RECORDED digest matches the raw file bytes — the expanded value is
        # NOT reflected in the record (expansion happens later, after parsing).
        raw = b"Greeting: ${GREETING}\n"
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "review.md").write_bytes(raw)
        workflow_path = _write_workflow(tmp_path)

        with patch.dict("os.environ", {"GREETING": "expanded-value"}):
            config, graph = load_config_with_graph(workflow_path)

        include = graph.files[1]
        assert include.digest == _sha256(raw)
        assert include.digest != _sha256(b"Greeting: expanded-value\n")
        # The loaded content still has the variable expanded.
        assert "expanded-value" in config.agents[0].prompt

    @pytest.mark.skipif(os.name == "nt", reason="Symlink semantics require POSIX privileges")
    def test_symlink_include_preserves_anchored_and_resolved_paths(self, tmp_path: Path) -> None:
        # Requirement: include provenance retains both the authored route and the read target.
        real = tmp_path / "real"
        real.mkdir()
        prompt = real / "review.md"
        prompt.write_text("review", encoding="utf-8")
        (tmp_path / "prompts").symlink_to(real, target_is_directory=True)
        workflow_path = _write_workflow(tmp_path)

        _config, graph = load_config_with_graph(workflow_path)

        include = graph.files[1]
        assert include.anchored_path == tmp_path / "prompts" / "review.md"
        assert include.path == prompt.resolve()


class TestNestedYamlfileChain:
    """Parent edges through a nested ``!yamlfile`` → ``!file`` chain."""

    def test_nested_yamlfile_then_file_records_parent_edges(self, tmp_path: Path) -> None:
        # Requirement: a !yamlfile include whose own YAML contains a nested
        # !file include produces both records, each with the correct parent —
        # the nested record's parent is the resolved !yamlfile path, not the
        # root workflow.
        leaf_raw = b"leaf content from a nested inclusion chain\n"
        (tmp_path / "descriptions").mkdir()
        (tmp_path / "descriptions" / "summary.md").write_bytes(leaf_raw)
        (tmp_path / "schema").mkdir()
        output_yaml = tmp_path / "schema" / "output.yaml"
        output_raw = b"summary:\n  type: string\n  description: !file ../descriptions/summary.md\n"
        output_yaml.write_bytes(output_raw)

        workflow_path = tmp_path / "workflow.yaml"
        workflow_path.write_text(
            """\
workflow:
  name: nested-graph-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    output: !yamlfile schema/output.yaml
    routes:
      - to: $end
"""
        )

        config, graph = load_config_with_graph(workflow_path)

        assert len(graph) == 3
        root, yamlfile_entry, file_entry = graph.files
        assert root.tag == "workflow"
        assert yamlfile_entry.tag == "yamlfile"
        assert yamlfile_entry.logical_ref == "schema/output.yaml"
        assert yamlfile_entry.path == output_yaml.resolve()
        assert yamlfile_entry.parent == workflow_path.resolve()
        assert yamlfile_entry.digest == _sha256(output_raw)
        assert file_entry.tag == "file"
        assert file_entry.logical_ref == "../descriptions/summary.md"
        assert file_entry.path == (tmp_path / "descriptions" / "summary.md").resolve()
        assert file_entry.parent == output_yaml.resolve()
        assert file_entry.digest == _sha256(leaf_raw)

        description = config.agents[0].output["summary"].description
        assert "leaf content" in description


class TestNoDeduplication:
    """Every read is recorded, even when the same file is included twice."""

    def test_same_file_included_twice_recorded_twice(self, tmp_path: Path) -> None:
        # Requirement: there is NO deduplication in the recorder — including
        # the same file twice yields two records (dedupe is the collector's
        # concern, per the run-bundle plan).
        raw = b"shared prompt content\n"
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "review.md").write_bytes(raw)
        workflow_path = tmp_path / "workflow.yaml"
        workflow_path.write_text(
            """\
workflow:
  name: dup-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: !file prompts/review.md
    system_prompt: !file prompts/review.md
    routes:
      - to: $end
"""
        )

        _config, graph = load_config_with_graph(workflow_path)

        assert len(graph) == 3
        includes = [f for f in graph if f.tag == "file"]
        assert len(includes) == 2
        assert all(f.path == (tmp_path / "prompts" / "review.md").resolve() for f in includes)
        assert all(f.digest == _sha256(raw) for f in includes)


class TestRootRecord:
    """The root workflow file is always node zero of the graph."""

    def test_root_recorded_first_with_workflow_tag(self, tmp_path: Path) -> None:
        # Requirement: the root workflow file is recorded FIRST with
        # tag="workflow" and parent=None, and its digest is over the raw
        # bytes of the workflow file itself.
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "review.md").write_text("prompt\n")
        workflow_path = _write_workflow(tmp_path)

        _config, graph = load_config_with_graph(workflow_path)

        root = graph.files[0]
        assert root.tag == "workflow"
        assert root.parent is None
        assert root.path == workflow_path.resolve()
        assert root.digest == _sha256(workflow_path.read_bytes())
        assert root.size == len(workflow_path.read_bytes())
        assert isinstance(root, IncludedFile)


class TestCrlfParity:
    """CRLF handling: universal newlines on load, raw-byte digests."""

    def test_crlf_universal_newlines_and_raw_digest(self, tmp_path: Path) -> None:
        # Requirement (CRLF parity): a file with CRLF line endings loads to
        # identical content semantics as before the change (universal
        # newlines — the loaded content contains no ``\\r``), while the
        # recorded digest is computed over the raw CRLF bytes. The same file
        # after dos2unix yields a different digest but the same loaded
        # content.
        crlf_raw = b"first line\r\nsecond line\r\n"
        lf_raw = b"first line\nsecond line\n"
        (tmp_path / "prompts").mkdir(exist_ok=True)
        prompt_file = tmp_path / "prompts" / "review.md"
        prompt_file.write_bytes(crlf_raw)
        workflow_path = _write_workflow(tmp_path)

        config, graph = load_config_with_graph(workflow_path)
        include = graph.files[1]

        # Loaded content matches the pre-change text-mode semantics.
        assert config.agents[0].prompt == "first line\nsecond line\n"
        assert "\r" not in config.agents[0].prompt
        # Digest is over the raw CRLF bytes, not the normalized content.
        assert include.digest == _sha256(crlf_raw)
        assert include.digest != _sha256(lf_raw)

        # dos2unix: same loaded content, different digest.
        prompt_file.write_bytes(lf_raw)
        config2, graph2 = load_config_with_graph(workflow_path)
        include2 = graph2.files[1]
        assert config2.agents[0].prompt == config.agents[0].prompt
        assert include2.digest == _sha256(lf_raw)
        assert include2.digest != include.digest

    def test_crlf_root_workflow_raw_digest(self, tmp_path: Path) -> None:
        # Requirement: the SAME raw-bytes transformation applies to the
        # root-file read in load() — the root digest is over the raw CRLF
        # bytes of the workflow file, and a CRLF workflow still loads fine.
        crlf_workflow = (
            _WORKFLOW_TEMPLATE.format(prompt_ref="prompts/review.md").replace("\n", "\r\n").encode()
        )
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "review.md").write_text("prompt\n")
        workflow_path = tmp_path / "workflow.yaml"
        workflow_path.write_bytes(crlf_workflow)

        config, graph = load_config_with_graph(workflow_path)

        assert config.workflow.name == "graph-test"
        root = graph.files[0]
        assert root.digest == _sha256(crlf_workflow)
        assert len(graph) == 2


class TestFailureSemanticsPreserved:
    """Cycle and missing-include errors keep their existing messages."""

    def test_missing_include_error_message_unchanged(self, tmp_path: Path) -> None:
        # Requirement: a missing include still raises ConfigurationError with
        # the same message shape ("File not found" naming the raw logical ref
        # and the resolved path).
        workflow_path = _write_workflow(tmp_path, prompt_ref="nonexistent.md")

        with pytest.raises(ConfigurationError) as exc_info:
            load_config_with_graph(workflow_path)

        message = str(exc_info.value)
        assert "File not found" in message
        assert "nonexistent.md" in message
        assert str(workflow_path.parent.resolve() / "nonexistent.md") in message

    def test_include_cycle_error_message_unchanged(self, tmp_path: Path) -> None:
        # Requirement: a circular include chain still raises
        # ConfigurationError with the same "Circular file reference" message,
        # and no partial include records escape to the caller.
        (tmp_path / "cycle_a.yaml").write_text("inner: !yamlfile cycle_b.yaml\n")
        (tmp_path / "cycle_b.yaml").write_text("inner: !yamlfile cycle_a.yaml\n")
        workflow_path = tmp_path / "workflow.yaml"
        workflow_path.write_text(
            """\
workflow:
  name: cycle-test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: !yamlfile cycle_a.yaml
    routes:
      - to: $end
"""
        )

        loader = ConfigLoader()
        with pytest.raises(ConfigurationError) as exc_info:
            loader.load_with_graph(workflow_path)

        message = str(exc_info.value)
        assert "Circular file reference" in message
        assert "cycle_a.yaml" in message
        # The recorder state is reset on the failure path too.
        assert loader._constructor_cls._included_files == []


class TestParity:
    """The plain load path and the graph path produce identical configs."""

    def test_load_config_parity_without_graph(self, tmp_path: Path) -> None:
        # Requirement (parity): load_config(p) equals
        # load_config_with_graph(p)[0] by model_dump() equality — the graph
        # API is purely additive.
        raw = b"Parity prompt with ${PLACEHOLDER}.\n"
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "review.md").write_bytes(raw)
        workflow_path = _write_workflow(tmp_path)

        with patch.dict("os.environ", {"PLACEHOLDER": "value"}):
            plain = load_config(workflow_path)
            with_graph, graph = load_config_with_graph(workflow_path)

        assert plain.model_dump() == with_graph.model_dump()
        assert len(graph) == 2


class TestNoExtraFileIO:
    """Loading without the graph performs zero additional file reads."""

    def test_no_extra_io_without_graph(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement (spy): loading WITHOUT the graph does ZERO extra file
        # I/O. The read_bytes↔read_text replacement is 1:1, so load_config on
        # a workflow with one !file include performs exactly two raw-byte
        # reads (root + include), zero text reads, and zero open() calls —
        # the same read count as before the change.
        raw = b"spy prompt\n"
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "review.md").write_bytes(raw)
        workflow_path = _write_workflow(tmp_path)

        counts = {"read_bytes": 0, "read_text": 0, "open": 0}
        original_read_bytes = Path.read_bytes
        original_read_text = Path.read_text
        original_open = builtins.open

        def counting_read_bytes(self: Path) -> bytes:
            counts["read_bytes"] += 1
            return original_read_bytes(self)

        def counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
            counts["read_text"] += 1
            return original_read_text(self, *args, **kwargs)

        def counting_open(*args: object, **kwargs: object) -> object:
            counts["open"] += 1
            return original_open(*args, **kwargs)

        monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
        monkeypatch.setattr(Path, "read_text", counting_read_text)
        monkeypatch.setattr(builtins, "open", counting_open)

        config = load_config(workflow_path)

        assert "spy prompt" in config.agents[0].prompt
        assert counts == {"read_bytes": 2, "read_text": 0, "open": 0}

    def test_load_string_records_root_and_includes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: load_string(source_path=...) records the root workflow
        # file as node zero as well, once the path is known. The recorder
        # state is reset when the call returns, so this test observes the
        # constructed IncludedFile records through a factory spy.
        import conductor.config.loader as loader_module

        raw = b"string-load prompt\n"
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "review.md").write_bytes(raw)
        content = _WORKFLOW_TEMPLATE.format(prompt_ref="prompts/review.md")
        source_path = tmp_path / "workflow.yaml"

        recorded: list[IncludedFile] = []
        real_included_file = loader_module.IncludedFile

        def factory_spy(*args: object, **kwargs: object) -> IncludedFile:
            instance = real_included_file(*args, **kwargs)
            recorded.append(instance)
            return instance

        monkeypatch.setattr(loader_module, "IncludedFile", factory_spy)

        loader = ConfigLoader()
        config = loader.load_string(content, source_path=source_path)

        assert len(recorded) == 2
        root, include = recorded
        assert root.tag == "workflow"
        assert root.parent is None
        assert root.path == source_path.resolve()
        # load_string does not re-read the file (zero extra I/O): its root
        # digest is over the UTF-8 encoding of the provided content.
        assert root.digest == _sha256(content.encode("utf-8"))
        assert include.tag == "file"
        assert include.parent == source_path.resolve()
        assert include.digest == _sha256(raw)
        assert "string-load prompt" in config.agents[0].prompt
