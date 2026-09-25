"""YAML configuration loader with environment variable resolution.

This module handles loading YAML workflow configuration files,
resolving environment variables, parsing them into typed
Pydantic models, and resolving ``!file`` tags for external file references.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from ruamel.yaml import YAML
from ruamel.yaml.constructor import RoundTripConstructor
from ruamel.yaml.error import YAMLError

from conductor.config.schema import WorkflowConfig
from conductor.exceptions import ConfigurationError
from conductor.file_string import FileString

# Pattern to match ${VAR} or ${VAR:-default}
ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")

# Tag of a recorded file read: the root workflow file, a ``!file`` include,
# or a ``!yamlfile`` include.
IncludedFileTag = Literal["workflow", "file", "yamlfile"]


@dataclass(frozen=True)
class IncludedFile:
    """Provenance record for one file read during a workflow load.

    Attributes:
        path: Resolved absolute path of the file that was read.
        anchored_path: Absolute parent-anchored path before symlink resolution,
            or ``None`` for records constructed by older integrations.
            This preserves the authored filesystem route so bundle collection can
            retain symlink components as well as the file reached through them.
        logical_ref: Raw reference string as written in the including YAML
            (before resolution against the including file's directory). For
            the root workflow file this is the path as passed to ``load()``.
        tag: Which construct read the file — the root ``workflow`` file, a
            ``file`` (``!file``) include, or a ``yamlfile`` (``!yamlfile``)
            include.
        parent: Resolved path of the file that included this one, or
            ``None`` for the root workflow file.
        digest: ``sha256:<hex>`` of the file's raw bytes as read from disk,
            computed before any ``${VAR}`` expansion or newline
            normalization.
        size: Length of the raw bytes in bytes.
    """

    path: Path
    logical_ref: str
    tag: IncludedFileTag
    parent: Path | None
    digest: str
    size: int
    anchored_path: Path | None = None


class IncludedFilesGraph:
    """The include-provenance records of a single load, in read order.

    The root workflow file is always recorded first (tag ``workflow``,
    ``parent=None``); every ``!file``/``!yamlfile`` include read afterwards
    is appended in the order it was read. Records are never deduplicated —
    the same file included twice appears twice.
    """

    def __init__(self, files: tuple[IncludedFile, ...]) -> None:
        """Initialize the graph with an immutable snapshot of the records.

        Args:
            files: The recorded ``IncludedFile`` entries in read order.
        """
        self._files = files

    @property
    def files(self) -> tuple[IncludedFile, ...]:
        """The recorded entries in read order."""
        return self._files

    def __iter__(self) -> Iterator[IncludedFile]:
        return iter(self._files)

    def __len__(self) -> int:
        return len(self._files)

    def __getitem__(self, index: int) -> IncludedFile:
        return self._files[index]


def _sha256_digest(raw: bytes) -> str:
    """Return the ``sha256:<hex>`` digest of raw bytes.

    Spelling matches the workflow-hash convention in ``engine.run_manifest``
    and the environment-document digest in ``config.environment``.
    """
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _normalize_newlines(content: str) -> str:
    """Apply universal-newline normalization to decoded text.

    Reproduces the semantics of reading in text mode with ``newline=None``
    (the previous ``read_text(encoding="utf-8")`` behavior): ``\\r\\n`` and
    lone ``\\r`` both become ``\\n``.
    """
    return content.replace("\r\n", "\n").replace("\r", "\n")


def resolve_env_vars(value: str, max_depth: int = 10) -> str:
    """Resolve ${ENV:-default} patterns in strings.

    Supports recursive resolution where environment variable values
    may themselves contain environment variable references.

    Args:
        value: The string potentially containing env var references.
        max_depth: Maximum recursion depth to prevent infinite loops.

    Returns:
        The string with all environment variables resolved.

    Raises:
        ConfigurationError: If a required environment variable is missing
            (no default provided) or recursion limit is exceeded.
    """
    if max_depth <= 0:
        raise ConfigurationError(
            f"Maximum recursion depth exceeded while resolving environment variables in: {value}",
            suggestion="Check for circular references in your environment variables.",
        )

    def replace_env_var(match: re.Match) -> str:
        var_name = match.group(1)
        default_value = match.group(2)

        env_value = os.environ.get(var_name)

        if env_value is not None:
            return env_value
        elif default_value is not None:
            return default_value
        else:
            raise ConfigurationError(
                f"Required environment variable '{var_name}' is not set",
                suggestion=f"Set the environment variable '{var_name}' or provide a default "
                f"value using the syntax: ${{{{{{var_name}}}}:-default_value}}",
            )

    # Perform substitution
    result = ENV_VAR_PATTERN.sub(replace_env_var, value)

    # Check if there are still env vars to resolve (recursive resolution)
    if ENV_VAR_PATTERN.search(result):
        return resolve_env_vars(result, max_depth - 1)

    return result


def _resolve_env_vars_recursive(data: Any) -> Any:
    """Recursively resolve environment variables in a data structure.

    Args:
        data: The data structure (dict, list, or scalar) to process.

    Returns:
        The data structure with all string values having env vars resolved.
    """
    if isinstance(data, dict):
        return {k: _resolve_env_vars_recursive(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_resolve_env_vars_recursive(item) for item in data]
    elif isinstance(data, FileString):
        return FileString(resolve_env_vars(data), data.source_path)
    elif isinstance(data, str):
        return resolve_env_vars(data)
    else:
        return data


class _FileTagConstructorType(Protocol):
    """Protocol for the dynamically-created FileTagConstructor class."""

    _base_dir: Path
    _file_stack: list[str]
    _included_files: list[IncludedFile]


def _create_file_tag_constructor_class() -> type[RoundTripConstructor]:
    """Create a per-instance RoundTripConstructor subclass with !file tag support.

    Returns a fresh subclass each time, so each ConfigLoader gets isolated
    mutable state (_base_dir, _file_stack) without cross-instance interference.

    Note: Cross-instance isolation is guaranteed because each ConfigLoader creates
    its own constructor subclass. However, concurrent calls on the *same* ConfigLoader
    instance are NOT thread-safe (the class-level _base_dir and _file_stack are shared
    mutable state). The convenience functions ``load_config()`` and ``load_config_string()``
    create a new loader per call, so they are safe for concurrent use.
    """

    class FileTagConstructor(RoundTripConstructor):
        """YAML constructor with !file tag for external file references."""

        _base_dir: Path = Path(".")
        _file_stack: list[str] = []
        _included_files: list[IncludedFile] = []

        def _read_included_file(self, node: Any, tag: IncludedFileTag) -> tuple[str, Path]:
            """Resolve a !file/!yamlfile path against the current base dir and read it.

            Reads the file's raw bytes once, computes the provenance digest and
            size over those bytes, then decodes and applies universal-newline
            normalization — exactly reproducing the previous
            ``read_text(encoding="utf-8")`` semantics.

            Raises ConfigurationError on a circular reference, a missing file, or
            invalid UTF-8. Returns the file's text content and its resolved path.
            """
            path_str = self.construct_scalar(node)
            cls = type(self)

            # Resolve path relative to the current base directory
            anchored_path = Path(os.path.abspath(os.path.normpath(cls._base_dir / path_str)))
            file_path = anchored_path.resolve()
            file_path_str = str(file_path)

            # Cycle detection (O(n) membership test, acceptable for small stacks)
            if file_path_str in cls._file_stack:
                chain = " → ".join(cls._file_stack) + " → " + file_path_str
                raise ConfigurationError(
                    f"Circular file reference detected: '{path_str}'\n"
                    f"  File inclusion chain: {chain}",
                    suggestion="Remove the circular !file reference.",
                )

            try:
                raw = file_path.read_bytes()
                content = raw.decode("utf-8")
            except FileNotFoundError as e:
                raise ConfigurationError(
                    f"File not found: '{path_str}' (resolved to '{file_path}')",
                    suggestion="Check the file path is correct relative to the workflow file "
                    "directory.",
                ) from e
            except UnicodeDecodeError as e:
                raise ConfigurationError(
                    f"Failed to read '{path_str}': file is not valid UTF-8 text ({e})",
                    suggestion="Ensure the file is saved as UTF-8 text.",
                ) from e

            # Record provenance BEFORE ${VAR} expansion (which happens later in
            # _resolve_env_vars_recursive) and BEFORE newline normalization
            # touches the decoded text. Digest and size are over the raw bytes.
            parent = Path(cls._file_stack[-1]) if cls._file_stack else None
            cls._included_files.append(
                IncludedFile(
                    path=file_path,
                    anchored_path=anchored_path,
                    logical_ref=path_str,
                    tag=tag,
                    parent=parent,
                    digest=_sha256_digest(raw),
                    size=len(raw),
                )
            )

            return _normalize_newlines(content), file_path

        def construct_file_tag(self, node: Any) -> Any:
            """Resolve a !file tag: always return the file's content verbatim as text.

            Never sniffs the content as YAML, so a prompt file's type can't
            silently flip between a string and a parsed mapping/list depending
            on whether its prose happens to parse as YAML. Use !yamlfile to
            explicitly parse a file as structured YAML.
            """
            content, file_path = self._read_included_file(node, "file")
            return FileString(content, source_path=file_path)

        def construct_yamlfile_tag(self, node: Any) -> Any:
            """Resolve a !yamlfile tag by parsing the referenced file as YAML.

            Supports nested !file/!yamlfile includes. Raises ConfigurationError
            if the file is not valid YAML, rather than silently falling back to
            a string the way the old !file content-sniffing did.
            """
            content, file_path = self._read_included_file(node, "yamlfile")
            cls = type(self)

            saved_base_dir = cls._base_dir
            cls._file_stack.append(str(file_path))
            try:
                cls._base_dir = file_path.parent
                sub_yaml = YAML()
                sub_yaml.Constructor = type(self)
                try:
                    return sub_yaml.load(content)
                except YAMLError as e:
                    raise ConfigurationError(
                        f"'{file_path}' is not valid YAML: {e}",
                        suggestion="Use !file instead of !yamlfile if this file is meant "
                        "to be loaded as plain text.",
                    ) from e
            finally:
                cls._base_dir = saved_base_dir
                cls._file_stack.pop()

    FileTagConstructor.add_constructor("!file", FileTagConstructor.construct_file_tag)
    FileTagConstructor.add_constructor("!yamlfile", FileTagConstructor.construct_yamlfile_tag)
    return FileTagConstructor


class ConfigLoader:
    """Loads and validates workflow configuration from YAML files.

    This class handles:
    - YAML parsing with line number tracking for error messages
    - Environment variable resolution
    - Pydantic schema validation
    """

    def __init__(self) -> None:
        """Initialize the config loader with a ruamel.yaml parser."""
        self._yaml = YAML()
        self._yaml.preserve_quotes = True
        self._constructor_cls: _FileTagConstructorType = _create_file_tag_constructor_class()
        self._yaml.Constructor = self._constructor_cls

    def load(self, path: str | Path) -> WorkflowConfig:
        """Load a workflow configuration from a YAML file.

        Args:
            path: Path to the YAML configuration file.

        Returns:
            A validated WorkflowConfig object.

        Raises:
            ConfigurationError: If the file cannot be read, contains invalid
                YAML syntax, or fails schema validation.
        """
        config, _graph = self.load_with_graph(path)
        return config

    def load_with_graph(self, path: str | Path) -> tuple[WorkflowConfig, IncludedFilesGraph]:
        """Load a workflow configuration and record include provenance.

        Behaves exactly like :meth:`load` (same file, error, and validation
        semantics) and additionally returns an :class:`IncludedFilesGraph`
        describing every file read: the root workflow file first, then each
        ``!file``/``!yamlfile`` include in read order.

        Args:
            path: Path to the YAML configuration file.

        Returns:
            A tuple of the validated WorkflowConfig object and the include
            provenance graph for this load.

        Raises:
            ConfigurationError: If the file cannot be read, contains invalid
                YAML syntax, or fails schema validation.
        """
        path = Path(path)

        if not path.exists():
            raise ConfigurationError(
                f"Workflow file not found: {path}",
                suggestion="Check that the file path is correct and the file exists.",
            )

        if not path.is_file():
            raise ConfigurationError(
                f"Path is not a file: {path}",
                suggestion="Provide a path to a YAML file, not a directory.",
            )

        try:
            raw = path.read_bytes()
        except OSError as e:
            raise ConfigurationError(
                f"Failed to read workflow file '{path}': {e}",
                suggestion="Check file permissions and ensure the file is readable.",
            ) from e

        # Set !file resolution state before loading, resetting the per-load
        # include recorder at the same point _base_dir/_file_stack are set.
        anchored = Path(os.path.abspath(os.path.normpath(path)))
        resolved = anchored.resolve()
        cls = self._constructor_cls
        cls._base_dir = resolved.parent
        cls._file_stack = [str(resolved)]
        cls._included_files = []
        # Record the root workflow file first: digest over its raw bytes,
        # before any parsing or ${VAR} expansion.
        cls._included_files.append(
            IncludedFile(
                path=resolved,
                anchored_path=anchored,
                logical_ref=str(path),
                tag="workflow",
                parent=None,
                digest=_sha256_digest(raw),
                size=len(raw),
            )
        )
        content = _normalize_newlines(raw.decode("utf-8"))
        try:
            config = self.load_string(content, source_path=path)
            graph = IncludedFilesGraph(tuple(cls._included_files))
            return config, graph
        finally:
            cls._base_dir = Path(".")
            cls._file_stack = []
            cls._included_files = []

    def load_string(self, content: str, source_path: Path | None = None) -> WorkflowConfig:
        """Load a workflow configuration from a YAML string.

        Args:
            content: The YAML content as a string.
            source_path: Optional path for error messages and !file resolution.

        Returns:
            A validated WorkflowConfig object.

        Raises:
            ConfigurationError: If the YAML is invalid or fails validation.
        """
        source = str(source_path) if source_path else "<string>"

        # Set !file resolution state if not already set by load()
        state_needs_reset = not self._constructor_cls._file_stack
        if state_needs_reset:
            self._constructor_cls._included_files = []
            if source_path is not None:
                anchored = Path(os.path.abspath(os.path.normpath(source_path)))
                resolved = anchored.resolve()
                self._constructor_cls._base_dir = resolved.parent
                self._constructor_cls._file_stack = [str(resolved)]
                # Record the root workflow file first. The raw bytes are not
                # re-read here (zero extra file I/O), so the digest is over
                # the UTF-8 encoding of the provided content; callers that
                # need raw-byte digests should use load()/load_config().
                encoded = content.encode("utf-8")
                self._constructor_cls._included_files.append(
                    IncludedFile(
                        path=resolved,
                        anchored_path=anchored,
                        logical_ref=str(source_path),
                        tag="workflow",
                        parent=None,
                        digest=_sha256_digest(encoded),
                        size=len(encoded),
                    )
                )
            else:
                self._constructor_cls._base_dir = Path.cwd()
                self._constructor_cls._file_stack = []

        try:
            try:
                data = self._yaml.load(content)
            except YAMLError as e:
                # Extract line number from the YAML error if available
                line_info = ""
                if hasattr(e, "problem_mark") and e.problem_mark is not None:
                    mark = e.problem_mark
                    # Access YAML marker attributes (dynamic type from ruamel.yaml)
                    line_info = f" at line {mark.line + 1}, column {mark.column + 1}"  # type: ignore[union-attr]

                raise ConfigurationError(
                    f"Invalid YAML syntax in '{source}'{line_info}: {e}",
                    suggestion="Check the YAML syntax. Common issues include incorrect "
                    "indentation, missing colons, or unquoted special characters.",
                ) from e

            if data is None:
                raise ConfigurationError(
                    f"Empty configuration file: {source}",
                    suggestion="Add workflow configuration to the YAML file.",
                )

            if not isinstance(data, dict):
                raise ConfigurationError(
                    f"Invalid configuration format in '{source}': "
                    f"expected a mapping, got {type(data).__name__}",
                    suggestion="Ensure the YAML file contains a valid workflow configuration.",
                )

            # Resolve environment variables
            try:
                data = _resolve_env_vars_recursive(data)
            except ConfigurationError:
                raise
            except Exception as e:
                raise ConfigurationError(
                    f"Failed to resolve environment variables in '{source}': {e}",
                    suggestion="Check the environment variable syntax. "
                    "Use ${VAR_NAME} or ${VAR_NAME:-default_value}.",
                ) from e

            # Validate against Pydantic schema
            return self._validate(data, source)
        finally:
            if state_needs_reset:
                self._constructor_cls._base_dir = Path(".")
                self._constructor_cls._file_stack = []
                self._constructor_cls._included_files = []

    def _validate(self, data: dict[str, Any], source: str) -> WorkflowConfig:
        """Validate configuration data against the Pydantic schema.

        Args:
            data: The parsed and env-var-resolved configuration data.
            source: The source file path for error messages.

        Returns:
            A validated WorkflowConfig object.

        Raises:
            ConfigurationError: If the data fails schema validation.
        """
        try:
            return WorkflowConfig.model_validate(data)
        except Exception as e:
            # Format Pydantic validation errors nicely
            error_msg = str(e)

            # Try to extract field path from Pydantic error
            if hasattr(e, "errors") and callable(e.errors):
                errors_result = e.errors()  # type: ignore[operator]
                if errors_result and isinstance(errors_result, list):
                    formatted_errors: list[str] = []
                    for err in errors_result:
                        if isinstance(err, dict):
                            loc_parts = err.get("loc", [])  # type: ignore[call-overload]
                            loc = ".".join(str(x) for x in loc_parts)
                            msg = err.get("msg", "Unknown error")  # type: ignore[call-overload]
                            formatted_errors.append(f"  - {loc}: {msg}")
                    if formatted_errors:
                        error_msg = "\n".join(formatted_errors)

            raise ConfigurationError(
                f"Configuration validation failed in '{source}':\n{error_msg}",
                suggestion="Check the workflow configuration against the schema. "
                "Ensure all required fields are present and have valid values.",
            ) from e


def load_config(path: str | Path) -> WorkflowConfig:
    """Convenience function to load a workflow configuration.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        A validated WorkflowConfig object.

    Raises:
        ConfigurationError: If loading or validation fails.
    """
    loader = ConfigLoader()
    return loader.load(path)


def load_config_with_graph(path: str | Path) -> tuple[WorkflowConfig, IncludedFilesGraph]:
    """Load a workflow configuration and return its include provenance graph.

    Behaves exactly like :func:`load_config` — same file, error, and
    validation semantics, with zero additional file I/O — and additionally
    returns an :class:`IncludedFilesGraph` recording the root workflow file
    (first) and every ``!file``/``!yamlfile`` include in read order.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        A tuple of the validated WorkflowConfig object and the include
        provenance graph for this load.

    Raises:
        ConfigurationError: If loading or validation fails.
    """
    loader = ConfigLoader()
    return loader.load_with_graph(path)


def load_config_string(content: str, source_path: Path | None = None) -> WorkflowConfig:
    """Convenience function to load a workflow configuration from a string.

    Args:
        content: The YAML content as a string.
        source_path: Optional path for error messages.

    Returns:
        A validated WorkflowConfig object.

    Raises:
        ConfigurationError: If loading or validation fails.
    """
    loader = ConfigLoader()
    return loader.load_string(content, source_path)


# Alias for backward compatibility
load_workflow = load_config
