"""Tests for ``conductor.config.environment`` — environment documents, discovery, resolution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from conductor.config.environment import (
    AVAILABLE_BACKEND_NAMES,
    DockerProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    _document_digest,
    builtin_local_environment,
    discover_all_environments,
    is_path_reference,
    load_environment_document,
    resolve_environment,
)
from conductor.exceptions import ConfigurationError

_VALID_DOCUMENT = """\
default: default
profiles:
  default:
    backend: local
"""


def _write(path: Path, content: str) -> Path:
    """Write ``content`` to ``path``, creating parents, and return ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _repo_with_marker(tmp_path: Path, marker_is_file: bool = False) -> Path:
    """Create ``repo/sub`` beneath ``tmp_path`` with a ``.git`` marker at ``repo``.

    Returns ``repo/sub`` as the workflow directory. With ``marker_is_file``
    the marker is a worktree-style pointer file instead of a directory.
    """
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    marker = repo / ".git"
    if marker_is_file:
        marker.write_text("gitdir: /elsewhere\n", encoding="utf-8")
    else:
        marker.mkdir()
    return sub


class TestProfileDefinition:
    """Requirements for the per-profile schema."""

    def test_local_backend_accepted(self) -> None:
        # Requirement: the only backend compiled into this build validates.
        profile = ProfileDefinition(backend="local")
        assert profile.backend == "local"
        assert "local" in AVAILABLE_BACKEND_NAMES

    def test_unknown_backend_names_available(self) -> None:
        # Requirement: an unavailable backend is rejected with a message naming
        # the backend and the available set — 'podman' must name 'aca, docker, local'.
        with pytest.raises(ValidationError) as exc_info:
            ProfileDefinition(backend="podman")
        message = str(exc_info.value)
        assert "execution backend 'podman' is not available in this build of Conductor" in message
        assert "available: aca, docker, local" in message

    def test_extra_field_forbidden(self) -> None:
        # Requirement: profiles carry exactly the specified fields — no
        # forward-declared options like 'image' or 'version' slip in.
        with pytest.raises(ValidationError):
            ProfileDefinition(backend="local", image="python:3.12")


class TestEnvironmentDocument:
    """Requirements for the document schema."""

    def test_default_must_name_existing_profile(self) -> None:
        # Requirement: a 'default' pointing outside 'profiles' fails at load
        # time — otherwise every profile-less workflow dies mid-run instead.
        with pytest.raises(ValidationError, match="default profile 'missing' is not defined"):
            EnvironmentDocument(
                default="missing",
                profiles={"default": ProfileDefinition(backend="local")},
            )

    def test_empty_profiles_rejected(self) -> None:
        # Requirement: a document with no profiles can satisfy nothing.
        with pytest.raises(ValidationError):
            EnvironmentDocument(profiles={})

    def test_extra_key_forbidden(self) -> None:
        # Requirement: no 'version', 'description', or merge keys — YAGNI.
        with pytest.raises(ValidationError):
            EnvironmentDocument(
                version=1,
                profiles={"default": ProfileDefinition(backend="local")},
            )

    def test_default_optional(self) -> None:
        # Requirement: authored documents may omit 'default'; only the
        # built-in environment is required to set it.
        document = EnvironmentDocument(profiles={"default": ProfileDefinition(backend="local")})
        assert document.default is None


class TestBuiltinLocalEnvironment:
    """Requirements for the built-in fallback environment."""

    def test_shape(self) -> None:
        # Requirement: the built-in environment is the local/default document
        # with the MANDATORY 'default' key, no file behind it.
        resolved = builtin_local_environment()
        assert resolved.name == "local/default"
        assert resolved.source == "builtin"
        assert resolved.path is None
        assert resolved.document.default == "default"
        assert resolved.document.profiles["default"].backend == "local"

    def test_digest_stable_across_calls(self) -> None:
        # Requirement: the built-in digest is content-derived, so two calls
        # must agree byte-for-byte (it pins the manifest's environment).
        first = builtin_local_environment()
        second = builtin_local_environment()
        assert first.digest == second.digest
        assert first.document == second.document

    def test_digest_matches_canonical_serialization(self) -> None:
        # Requirement: digest = sha256 of canonical JSON (sorted keys, tight
        # separators) over model_dump(mode="json"), spelled 'sha256:<hex>'.
        resolved = builtin_local_environment()
        canonical = json.dumps(
            resolved.document.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        expected = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
        assert resolved.digest == expected


class TestLoadEnvironmentDocument:
    """Requirements for plain-YAML loading."""

    def test_loads_valid_document(self, tmp_path: Path) -> None:
        # Requirement: a well-formed document round-trips into the schema.
        path = _write(tmp_path / "envs" / "prod.yaml", _VALID_DOCUMENT)
        document = load_environment_document(path)
        assert document.default == "default"
        assert document.profiles["default"].backend == "local"

    def test_malformed_yaml_raises_with_file_path(self, tmp_path: Path) -> None:
        # Requirement: malformed YAML raises ConfigurationError naming the
        # offending file via file_path (settings.py error style).
        path = _write(tmp_path / "broken.yaml", "profiles: [unclosed\n")
        with pytest.raises(ConfigurationError) as exc_info:
            load_environment_document(path)
        assert exc_info.value.file_path == str(path)
        assert str(path) in str(exc_info.value)

    def test_schema_error_raises_with_file_path(self, tmp_path: Path) -> None:
        # Requirement: well-formed YAML failing schema validation is also a
        # ConfigurationError naming file_path, not a raw pydantic leak.
        path = _write(tmp_path / "bad-backend.yaml", "profiles:\n  p:\n    backend: docker\n")
        with pytest.raises(ConfigurationError) as exc_info:
            load_environment_document(path)
        assert exc_info.value.file_path == str(path)

    def test_invalid_utf8_raises_configuration_error_naming_path(self, tmp_path: Path) -> None:
        # Requirement: a document that is not valid UTF-8 raises
        # ConfigurationError naming the path and the UTF-8 failure — not a
        # raw UnicodeDecodeError — with an encoding-specific fix suggestion.
        path = tmp_path / "latin1.yaml"
        path.write_bytes(b"default: default\nprofiles:\n  default:\n    backend: local\n\xff\n")
        with pytest.raises(ConfigurationError) as exc_info:
            load_environment_document(path)
        assert exc_info.value.file_path == str(path)
        assert "UTF-8" in str(exc_info.value)
        assert str(path) in str(exc_info.value)

    def test_no_env_var_expansion(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Requirement: ${VAR} is NOT expanded in environment documents — the
        # literal '${EP_ENV_NAME}' reaches the schema and fails validation,
        # proving no silent machine-dependent expansion happened.
        monkeypatch.setenv("EP_ENV_NAME", "default")
        path = _write(
            tmp_path / "env.yaml",
            "default: ${EP_ENV_NAME}\nprofiles:\n  default:\n    backend: local\n",
        )
        with pytest.raises(ConfigurationError) as exc_info:
            load_environment_document(path)
        assert "EP_ENV_NAME" in str(exc_info.value)


class TestIsPathReference:
    """Requirements for name-vs-path classification."""

    def test_tilde_is_path(self) -> None:
        # Requirement (QA scenario): '~' is classified as a path reference.
        assert is_path_reference("~/environments/prod.yaml")

    def test_dot_prefix_is_path(self) -> None:
        # Requirement: relative dot-prefixed values anchor at the workflow.
        assert is_path_reference("./prod.yaml")
        assert is_path_reference("../prod.yaml")

    def test_separator_is_path(self) -> None:
        # Requirement: any path separator (POSIX or Windows) means path —
        # names never contain separators or dots.
        assert is_path_reference("envs/prod.yaml")
        assert is_path_reference("envs\\prod.yaml")

    def test_absolute_is_path(self) -> None:
        # Requirement: absolute POSIX paths classify as paths.
        assert is_path_reference("/etc/conductor/prod.yaml")

    def test_plain_name_is_not_path(self) -> None:
        # Requirement: the [A-Za-z0-9_-]+ name charset classifies as a name.
        assert not is_path_reference("prod")
        assert not is_path_reference("my-env")
        assert not is_path_reference("my_env")
        assert not is_path_reference("team1")


class TestResolveEnvironment:
    """Requirements for name/path resolution."""

    def test_nearest_ancestor_wins(self, tmp_path: Path) -> None:
        # Requirement (QA scenario): name resolution walks the project chain
        # and the NEAREST ancestor's document wins over the repo root's.
        workflow_dir = _repo_with_marker(tmp_path)
        root_doc = _write(
            tmp_path / "repo" / ".conductor" / "environments" / "prod.yaml",
            "profiles:\n  root_only:\n    backend: local\n",
        )
        near_doc = _write(
            tmp_path / "repo" / "sub" / ".conductor" / "environments" / "prod.yaml",
            "profiles:\n  near_only:\n    backend: local\n",
        )
        resolved = resolve_environment("prod", workflow_dir=workflow_dir)
        assert resolved.source == "project"
        assert resolved.path == near_doc
        assert "near_only" in resolved.document.profiles
        assert "root_only" not in resolved.document.profiles
        assert root_doc.exists()  # the shadowed document is untouched on disk

    def test_git_directory_stops_walk(self, tmp_path: Path) -> None:
        # Requirement (QA scenario): a .git DIRECTORY stops the walk — a name
        # existing only ABOVE the repo root must not resolve.
        workflow_dir = _repo_with_marker(tmp_path)
        _write(
            tmp_path / ".conductor" / "environments" / "above.yaml",
            _VALID_DOCUMENT,
        )
        _write(tmp_path / "repo" / ".conductor" / "environments" / "prod.yaml", _VALID_DOCUMENT)
        assert resolve_environment("prod", workflow_dir=workflow_dir).source == "project"
        with pytest.raises(ConfigurationError) as exc_info:
            resolve_environment("above", workflow_dir=workflow_dir)
        assert str(tmp_path / ".conductor") not in str(exc_info.value)

    def test_git_file_stops_walk(self, tmp_path: Path) -> None:
        # Requirement (QA scenario): a .git FILE (linked worktree pointer)
        # stops the walk exactly like a directory.
        workflow_dir = _repo_with_marker(tmp_path, marker_is_file=True)
        _write(
            tmp_path / ".conductor" / "environments" / "above.yaml",
            _VALID_DOCUMENT,
        )
        _write(tmp_path / "repo" / ".conductor" / "environments" / "prod.yaml", _VALID_DOCUMENT)
        assert resolve_environment("prod", workflow_dir=workflow_dir).source == "project"
        with pytest.raises(ConfigurationError):
            resolve_environment("above", workflow_dir=workflow_dir)

    def test_user_level_fallback_via_conductor_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement (QA scenario): with no project match, the user level
        # under $CONDUCTOR_HOME resolves (the module must honor the env var,
        # never read the real home in tests).
        home = tmp_path / "isolated-home"
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))
        user_doc = _write(home / "environments" / "prod.yaml", _VALID_DOCUMENT)
        workflow_dir = _repo_with_marker(tmp_path)
        resolved = resolve_environment("prod", workflow_dir=workflow_dir)
        assert resolved.source == "user"
        assert resolved.path == user_doc
        assert resolved.document.default == "default"

    def test_path_reference_loads_directly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: a path resolves with source='path' and name = stem,
        # bypassing the name chain entirely. A relative path anchors at the
        # process cwd (callers absolutise before detached forwarding), and is
        # reported verbatim.
        workflow_dir = _repo_with_marker(tmp_path)
        _write(workflow_dir / "custom.yaml", _VALID_DOCUMENT)
        monkeypatch.chdir(workflow_dir)
        resolved = resolve_environment("./custom.yaml", workflow_dir=workflow_dir)
        assert resolved.source == "path"
        assert resolved.name == "custom"
        assert resolved.path == Path("custom.yaml")
        assert resolved.document.default == "default"

    def test_tilde_path_is_expanded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Requirement: a '~'-classified path is expanded before loading, so
        # the classification from is_path_reference is actually usable.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        doc = _write(tmp_path / "envs" / "prod.yaml", _VALID_DOCUMENT)
        resolved = resolve_environment("~/envs/prod.yaml", workflow_dir=tmp_path)
        assert resolved.source == "path"
        assert resolved.path == doc

    def test_missing_name_lists_searched_locations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement (QA scenario): a missing name raises ConfigurationError
        # enumerating every searched location (project chain + user level).
        home = tmp_path / "isolated-home"
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))
        workflow_dir = _repo_with_marker(tmp_path)
        with pytest.raises(ConfigurationError) as exc_info:
            resolve_environment("prod", workflow_dir=workflow_dir)
        message = str(exc_info.value)
        assert "'prod'" in message
        assert str(workflow_dir / ".conductor" / "environments" / "prod.yaml") in message
        assert str(workflow_dir.parent / ".conductor" / "environments" / "prod.yaml") in message
        assert str(home / "environments" / "prod.yaml") in message

    def test_name_outside_charset_rejected(self, tmp_path: Path) -> None:
        # Requirement: a name outside the [A-Za-z0-9_-]+ charset raises
        # ConfigurationError naming the value and the charset (instead of
        # silently mapping it to '<name>.yaml'); boundary-valid names still
        # resolve through the same chain.
        workflow_dir = _repo_with_marker(tmp_path)
        for bad_name in ("bad name", "with.dot"):
            with pytest.raises(ConfigurationError) as exc_info:
                resolve_environment(bad_name, workflow_dir=workflow_dir)
            message = str(exc_info.value)
            assert bad_name in message
            assert "A-Za-z0-9_-" in message
        _write(
            tmp_path / "repo" / ".conductor" / "environments" / "ok-name_1.yaml",
            _VALID_DOCUMENT,
        )
        assert resolve_environment("ok-name_1", workflow_dir=workflow_dir).source == "project"

    def test_explicit_resolution_of_malformed_document_is_fatal(self, tmp_path: Path) -> None:
        # Requirement: bare discovery skips malformed files, but explicitly
        # resolving that name is fatal — explicit resolution promises a
        # loadable document.
        workflow_dir = _repo_with_marker(tmp_path)
        _write(
            tmp_path / "repo" / ".conductor" / "environments" / "broken.yaml",
            "profiles: [unclosed\n",
        )
        with pytest.raises(ConfigurationError):
            resolve_environment("broken", workflow_dir=workflow_dir)

    def test_explicit_resolution_of_invalid_utf8_document_is_fatal(self, tmp_path: Path) -> None:
        # Requirement (PR #551 review): explicit resolution of a bad-encoded
        # document surfaces a contextual ConfigurationError (path + UTF-8),
        # not a raw UnicodeDecodeError traceback.
        workflow_dir = _repo_with_marker(tmp_path)
        (tmp_path / "repo" / ".conductor" / "environments").mkdir(parents=True)
        (tmp_path / "repo" / ".conductor" / "environments" / "broken.yaml").write_bytes(
            b"profiles:\n  p:\n    backend: local\n\xff\n"
        )
        with pytest.raises(ConfigurationError, match="UTF-8"):
            resolve_environment("broken", workflow_dir=workflow_dir)

    def test_relative_workflow_dir_from_subdirectory_finds_repo_root_document(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement (PR #551 review, blocking): a RELATIVE workflow
        # directory (Path('.') when the CLI runs from a subdirectory) has no
        # .parents, so without normalization the walk would miss the repo
        # root document entirely. The root project document must win over a
        # same-named user document — never the silent user fallback.
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "isolated-home"))
        sub = _repo_with_marker(tmp_path)
        _write(
            tmp_path / "repo" / ".conductor" / "environments" / "prod.yaml",
            "profiles:\n  project_only:\n    backend: local\n",
        )
        _write(
            tmp_path / "isolated-home" / "environments" / "prod.yaml",
            "profiles:\n  user_only:\n    backend: local\n",
        )
        monkeypatch.chdir(sub)
        resolved = resolve_environment("prod", workflow_dir=Path("."))
        assert resolved.source == "project"
        assert "project_only" in resolved.document.profiles
        assert "user_only" not in resolved.document.profiles


class TestShadowing:
    """Negative controls for the no-merge policy."""

    def test_project_document_shadows_user_document_without_merge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement (QA scenario, negative control): a key present ONLY in
        # the same-named user-level document must NOT appear in the
        # project-resolved result — whole-document shadowing proves there is
        # no merge between documents.
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "isolated-home"))
        workflow_dir = _repo_with_marker(tmp_path)
        _write(
            tmp_path / "repo" / ".conductor" / "environments" / "prod.yaml",
            "profiles:\n  project_only:\n    backend: local\n",
        )
        _write(
            tmp_path / "isolated-home" / "environments" / "prod.yaml",
            "profiles:\n  user_only:\n    backend: local\n",
        )
        resolved = resolve_environment("prod", workflow_dir=workflow_dir)
        assert "project_only" in resolved.document.profiles
        assert "user_only" not in resolved.document.profiles


class TestDiscoverAll:
    """Requirements for bare-validate discovery."""

    def test_collects_project_and_user_documents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: discovery collects every well-formed document in the
        # project chain and the user level, keyed by name with sources set.
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "isolated-home"))
        workflow_dir = _repo_with_marker(tmp_path)
        _write(
            tmp_path / "repo" / ".conductor" / "environments" / "prod.yaml",
            _VALID_DOCUMENT,
        )
        _write(tmp_path / "isolated-home" / "environments" / "personal.yaml", _VALID_DOCUMENT)
        discovered = discover_all_environments(workflow_dir)
        assert set(discovered) == {"prod", "personal"}
        assert discovered["prod"].source == "project"
        assert discovered["personal"].source == "user"

    def test_nearest_document_wins_on_name_collision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: discovery applies the same whole-document shadowing —
        # the nearest occurrence of a name wins, still with no merge.
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "isolated-home"))
        workflow_dir = _repo_with_marker(tmp_path)
        _write(
            tmp_path / "repo" / ".conductor" / "environments" / "prod.yaml",
            "profiles:\n  project_only:\n    backend: local\n",
        )
        _write(
            tmp_path / "isolated-home" / "environments" / "prod.yaml",
            "profiles:\n  user_only:\n    backend: local\n",
        )
        discovered = discover_all_environments(workflow_dir)
        assert set(discovered) == {"prod"}
        assert "project_only" in discovered["prod"].document.profiles
        assert "user_only" not in discovered["prod"].document.profiles

    def test_malformed_file_warns_and_is_skipped(self, tmp_path: Path) -> None:
        # Requirement (QA scenario): a malformed document in bare mode warns
        # naming the file and is treated as non-resolving — never a hard error.
        workflow_dir = _repo_with_marker(tmp_path)
        broken = _write(
            tmp_path / "repo" / ".conductor" / "environments" / "broken.yaml",
            "profiles: [unclosed\n",
        )
        _write(tmp_path / "repo" / ".conductor" / "environments" / "good.yaml", _VALID_DOCUMENT)
        warnings: list[str] = []
        discovered = discover_all_environments(workflow_dir, on_warning=warnings.append)
        assert set(discovered) == {"good"}
        assert len(warnings) == 1
        assert str(broken) in warnings[0]

    def test_invalid_utf8_document_warns_and_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement (PR #551 review): one bad-encoded document must not
        # abort ambient discovery — it warns naming the file while the other
        # valid document still resolves.
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "isolated-home"))
        workflow_dir = _repo_with_marker(tmp_path)
        envs = tmp_path / "repo" / ".conductor" / "environments"
        envs.mkdir(parents=True)
        (envs / "broken.yaml").write_bytes(b"profiles:\n  p:\n    backend: local\n\xff\n")
        _write(envs / "good.yaml", _VALID_DOCUMENT)
        warnings: list[str] = []
        discovered = discover_all_environments(workflow_dir, on_warning=warnings.append)
        assert set(discovered) == {"good"}
        assert len(warnings) == 1
        assert "broken.yaml" in warnings[0]
        assert "UTF-8" in warnings[0]

    def test_walk_stops_at_repo_marker(self, tmp_path: Path) -> None:
        # Requirement: discovery never sweeps directories above the repository
        # root — documents there are invisible to the workflow.
        workflow_dir = _repo_with_marker(tmp_path)
        _write(tmp_path / ".conductor" / "environments" / "above.yaml", _VALID_DOCUMENT)
        _write(tmp_path / "repo" / ".conductor" / "environments" / "prod.yaml", _VALID_DOCUMENT)
        discovered = discover_all_environments(workflow_dir)
        assert set(discovered) == {"prod"}

    def test_no_documents_returns_empty(self, tmp_path: Path) -> None:
        # Requirement: an empty environment set is a normal result, not an
        # error — bare validation then warns that refs could not be checked.
        workflow_dir = _repo_with_marker(tmp_path)
        assert discover_all_environments(workflow_dir) == {}


class TestDockerRunnerImage:
    """Requirements for agent-realm image references and digest stability."""

    def test_docker_digest_unchanged_when_runner_image_is_unset(self) -> None:
        # Requirement: the optional field must not rewrite existing Docker digests.
        document = EnvironmentDocument.model_validate(
            {
                "default": "docker",
                "profiles": {
                    "docker": {
                        "backend": "docker",
                        "docker": {"image": "python:3.12"},
                    }
                },
            }
        )
        assert document.model_dump(mode="json")["profiles"]["docker"]["docker"] == {
            "image": "python:3.12",
            "platform": None,
            "network": None,
            "user": None,
            "init": False,
            "read_only": False,
            "cap_drop_all": False,
            "no_new_privileges": False,
            "tmpfs": False,
            "resources": {"cpu": None, "memory": None, "pids": None},
        }
        assert _document_digest(document) == (
            "sha256:876f5d8360ba9ba44d8fbf0f3f3bd89fd8618a59919943b764ca0ef1a68fdb1d"
        )

    def test_runner_image_is_parsed_and_included_in_digest(self, tmp_path: Path) -> None:
        # Requirement: an authored runner image is retained in canonical data and therefore pinned.
        path = _write(
            tmp_path / "runner.yaml",
            """\
            default: docker
            profiles:
              docker:
                backend: docker
                docker:
                  image: python:3.12
                  runner_image: registry.example/conductor-runner:1
            """,
        )
        document = load_environment_document(path)
        docker = document.profiles["docker"].docker
        assert docker is not None
        assert docker.runner_image == "registry.example/conductor-runner:1"
        assert (
            document.model_dump(mode="json")["profiles"]["docker"]["docker"]["runner_image"]
            == "registry.example/conductor-runner:1"
        )
        resolved = resolve_environment(str(path), workflow_dir=tmp_path)
        assert resolved.digest.startswith("sha256:")
        assert resolved.digest == _document_digest(document)

    @pytest.mark.parametrize("value", ["", "   "])
    def test_runner_image_rejects_empty_or_whitespace(self, value: str) -> None:
        # Requirement: a configured runner image must be non-empty and contain no whitespace.
        with pytest.raises(ValidationError, match="runner_image must be a non-empty string"):
            DockerProfileOptions(image="python:3.12", runner_image=value)

    def test_yaml_integer_runner_image_is_a_type_error(self, tmp_path: Path) -> None:
        # Requirement: YAML scalars of the wrong type are not coerced into image references.
        path = _write(
            tmp_path / "bad-runner.yaml",
            """\
            profiles:
              docker:
                backend: docker
                docker:
                  image: python:3.12
                  runner_image: 42
            """,
        )
        with pytest.raises(ConfigurationError, match="valid string"):
            load_environment_document(path)
