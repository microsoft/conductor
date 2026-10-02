"""Requirements and regression tests for Docker profile options in environment documents."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from conductor.config.environment import (
    DockerProfileOptions,
    DockerResources,
    EnvironmentDocument,
    ProfileDefinition,
    builtin_local_environment,
    load_environment_document,
)
from conductor.exceptions import ConfigurationError


def _write_yaml(path: Path, content: str) -> Path:
    """Helper to write a YAML file with parent directory creation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestDockerResources:
    """Requirements for DockerResources model."""

    def test_defaults_all_none(self) -> None:
        # Requirement: all resource constraints default to None (k8s model: unset = no limit).
        resources = DockerResources()
        assert resources.cpu is None
        assert resources.memory is None
        assert resources.pids is None

    def test_cpu_bounds_when_set(self) -> None:
        # Requirement: CPU limit is bounded between 0.1 and 64 cores when specified.
        assert DockerResources(cpu=0.1).cpu == 0.1
        assert DockerResources(cpu=64.0).cpu == 64.0
        assert DockerResources(cpu=2.5).cpu == 2.5

        with pytest.raises(ValidationError):
            DockerResources(cpu=0.09)

        with pytest.raises(ValidationError):
            DockerResources(cpu=64.1)

    def test_memory_bounds_and_normalization(self) -> None:
        # Requirement: Memory limit matches \d+[mg], lowercases, and bounds to 16m-64g.
        assert DockerResources(memory="16m").memory == "16m"
        assert DockerResources(memory="64g").memory == "64g"
        assert DockerResources(memory="512M").memory == "512m"
        assert DockerResources(memory="2G").memory == "2g"

        with pytest.raises(ValidationError, match="out of bounds"):
            DockerResources(memory="15m")

        with pytest.raises(ValidationError, match="out of bounds"):
            DockerResources(memory="65g")

        with pytest.raises(ValidationError, match="out of bounds"):
            DockerResources(memory="0m")

        with pytest.raises(ValidationError, match="pattern"):
            DockerResources(memory="invalid")

        with pytest.raises(ValidationError, match="pattern"):
            DockerResources(memory="10k")

    def test_pids_bounds_when_set(self) -> None:
        # Requirement: PIDs limit is bounded between 16 and 65536 when specified.
        assert DockerResources(pids=16).pids == 16
        assert DockerResources(pids=65536).pids == 65536

        with pytest.raises(ValidationError):
            DockerResources(pids=15)

        with pytest.raises(ValidationError):
            DockerResources(pids=65537)

    def test_extra_fields_forbidden(self) -> None:
        # Requirement: DockerResources forbids extra fields.
        with pytest.raises(ValidationError):
            DockerResources.model_validate({"cpu": 1.0, "unknown": 123})


class TestDockerProfileOptions:
    """Requirements for DockerProfileOptions model."""

    def test_image_shape_validation(self) -> None:
        # Requirement: image is non-empty, without whitespace (tags and digests allowed).
        assert DockerProfileOptions(image="ubuntu:latest").image == "ubuntu:latest"
        assert DockerProfileOptions(image="busybox").image == "busybox"
        assert DockerProfileOptions(image="repo/app:1.0.0").image == "repo/app:1.0.0"
        assert (
            DockerProfileOptions(
                image="alpine@sha256:e4355b6699da7bdff6dd602f9b50066b53fb37ef5ab34da79e3995603775b92d"
            ).image
            == "alpine@sha256:e4355b6699da7bdff6dd602f9b50066b53fb37ef5ab34da79e3995603775b92d"
        )
        assert DockerProfileOptions(image="  python:3.12  ").image == "python:3.12"

        with pytest.raises(ValidationError, match="non-empty string without whitespace"):
            DockerProfileOptions(image="")

        with pytest.raises(ValidationError, match="non-empty string without whitespace"):
            DockerProfileOptions(image="   ")

        with pytest.raises(ValidationError, match="non-empty string without whitespace"):
            DockerProfileOptions(image="ubuntu latest")

    def test_platform_validation(self) -> None:
        # Requirement: platform is optional Literal['linux/amd64', 'linux/arm64'].
        assert DockerProfileOptions(image="ubuntu", platform=None).platform is None
        assert (
            DockerProfileOptions(image="ubuntu", platform="linux/amd64").platform == "linux/amd64"
        )
        assert (
            DockerProfileOptions(image="ubuntu", platform="linux/arm64").platform == "linux/arm64"
        )

        with pytest.raises(ValidationError):
            DockerProfileOptions(image="ubuntu", platform="linux/386")  # type: ignore[arg-type]

    def test_network_validation(self) -> None:
        # Requirement: network is optional Literal['none', 'bridge', 'host'].
        assert DockerProfileOptions(image="ubuntu", network=None).network is None
        assert DockerProfileOptions(image="ubuntu", network="none").network == "none"
        assert DockerProfileOptions(image="ubuntu", network="bridge").network == "bridge"
        assert DockerProfileOptions(image="ubuntu", network="host").network == "host"

        with pytest.raises(ValidationError):
            DockerProfileOptions(image="ubuntu", network="custom")  # type: ignore[arg-type]

    def test_user_syntax_validation(self) -> None:
        # Requirement: user is optional and follows docker syntax <name|uid>[:<group|gid>].
        assert DockerProfileOptions(image="ubuntu", user=None).user is None
        assert DockerProfileOptions(image="ubuntu", user="root").user == "root"
        assert DockerProfileOptions(image="ubuntu", user="0:0").user == "0:0"
        assert DockerProfileOptions(image="ubuntu", user="1000").user == "1000"
        assert DockerProfileOptions(image="ubuntu", user="1000:1000").user == "1000:1000"
        assert DockerProfileOptions(image="ubuntu", user="node:node").user == "node:node"
        assert (
            DockerProfileOptions(image="ubuntu", user="my_user-1:my_group-2").user
            == "my_user-1:my_group-2"
        )

        with pytest.raises(ValidationError, match="docker user syntax"):
            DockerProfileOptions(image="ubuntu", user=":group")

        with pytest.raises(ValidationError, match="docker user syntax"):
            DockerProfileOptions(image="ubuntu", user="user:")

        with pytest.raises(ValidationError, match="docker user syntax"):
            DockerProfileOptions(image="ubuntu", user="user@domain")

        with pytest.raises(ValidationError, match="docker user syntax"):
            DockerProfileOptions(image="ubuntu", user="-user")

        with pytest.raises(ValidationError, match="docker user syntax"):
            DockerProfileOptions(image="ubuntu", user="user:group:extra")

    def test_defaults_and_boolean_flags(self) -> None:
        # Requirement: boolean hardening flags default to False (platform-native defaults).
        options = DockerProfileOptions(image="ubuntu")
        assert options.init is False
        assert options.read_only is False
        assert options.cap_drop_all is False
        assert options.no_new_privileges is False
        assert options.tmpfs is False
        assert isinstance(options.resources, DockerResources)

    def test_tmpfs_strict_typing_and_bounds(self) -> None:
        # Requirement: tmpfs accepts bool or size string (1m-16g); rejects invalid types.
        assert DockerProfileOptions(image="ubuntu", tmpfs=False).tmpfs is False
        assert DockerProfileOptions(image="ubuntu", tmpfs=True).tmpfs is True
        assert DockerProfileOptions(image="ubuntu", tmpfs="1m").tmpfs == "1m"
        assert DockerProfileOptions(image="ubuntu", tmpfs="512M").tmpfs == "512m"
        assert DockerProfileOptions(image="ubuntu", tmpfs="1g").tmpfs == "1g"
        assert DockerProfileOptions(image="ubuntu", tmpfs="16g").tmpfs == "16g"

        with pytest.raises(ValidationError, match="out of bounds"):
            DockerProfileOptions(image="ubuntu", tmpfs="0m")

        with pytest.raises(ValidationError, match="out of bounds"):
            DockerProfileOptions(image="ubuntu", tmpfs="17g")

        with pytest.raises(ValidationError, match="boolean or match pattern"):
            DockerProfileOptions(image="ubuntu", tmpfs="invalid")

        # Strict typing: integer / float / null / lists are rejected
        with pytest.raises(ValidationError, match="boolean or size string"):
            DockerProfileOptions.model_validate({"image": "ubuntu", "tmpfs": 1})

        with pytest.raises(ValidationError, match="boolean or size string"):
            DockerProfileOptions.model_validate({"image": "ubuntu", "tmpfs": 0})

        with pytest.raises(ValidationError, match="boolean or size string"):
            DockerProfileOptions.model_validate({"image": "ubuntu", "tmpfs": 1.0})

        with pytest.raises(ValidationError, match="boolean or size string"):
            DockerProfileOptions.model_validate({"image": "ubuntu", "tmpfs": None})

    def test_extra_fields_forbidden(self) -> None:
        # Requirement: DockerProfileOptions forbids extra fields.
        with pytest.raises(ValidationError):
            DockerProfileOptions.model_validate({"image": "ubuntu", "extra": "forbidden"})


class TestProfileDefinitionDocker:
    """Requirements for ProfileDefinition with backend='docker'."""

    def test_backend_docker_requires_docker_options(self) -> None:
        # Requirement: backend='docker' requires a 'docker' configuration block.
        with pytest.raises(ValidationError, match="requires a 'docker' configuration block"):
            ProfileDefinition(backend="docker")

    def test_backend_local_forbids_docker_options(self) -> None:
        # Requirement: backend!='docker' cannot specify a 'docker' configuration block.
        docker_opts = DockerProfileOptions(image="ubuntu")
        with pytest.raises(ValidationError, match="cannot specify a 'docker' configuration block"):
            ProfileDefinition(backend="local", docker=docker_opts)

    def test_backend_docker_with_valid_options(self) -> None:
        # Requirement: backend='docker' succeeds when valid 'docker' configuration is provided.
        docker_opts = DockerProfileOptions(image="ubuntu:latest")
        profile = ProfileDefinition(backend="docker", docker=docker_opts)
        assert profile.backend == "docker"
        assert profile.docker is not None
        assert profile.docker.image == "ubuntu:latest"


class TestEnvironmentDocumentDocker:
    """Requirements for EnvironmentDocument containing docker profiles."""

    def test_load_full_docker_environment_yaml(self, tmp_path: Path) -> None:
        # Requirement: full docker profile YAML parses and validates with all options.
        yaml_content = """
default: ci
profiles:
  ci:
    backend: docker
    inherit_control_environment: false
    docker:
      image: "python:3.12-slim"
      platform: "linux/amd64"
      network: "bridge"
      user: "1000:1000"
      init: true
      read_only: true
      cap_drop_all: true
      no_new_privileges: true
      tmpfs: "512m"
      resources:
        cpu: 2.0
        memory: "4g"
        pids: 512
"""
        doc_path = _write_yaml(tmp_path / "env.yaml", yaml_content)
        doc = load_environment_document(doc_path)
        assert doc.default == "ci"
        profile = doc.profiles["ci"]
        assert profile.backend == "docker"
        assert profile.inherit_control_environment is False
        assert profile.docker is not None
        assert profile.docker.image == "python:3.12-slim"
        assert profile.docker.platform == "linux/amd64"
        assert profile.docker.network == "bridge"
        assert profile.docker.user == "1000:1000"
        assert profile.docker.init is True
        assert profile.docker.read_only is True
        assert profile.docker.cap_drop_all is True
        assert profile.docker.no_new_privileges is True
        assert profile.docker.tmpfs == "512m"
        assert profile.docker.resources.cpu == 2.0
        assert profile.docker.resources.memory == "4g"
        assert profile.docker.resources.pids == 512

    def test_load_minimal_docker_environment_yaml(self, tmp_path: Path) -> None:
        # Requirement: minimal docker profile YAML with platform-native defaults succeeds.
        yaml_content = """
default: minimal
profiles:
  minimal:
    backend: docker
    docker:
      image: "busybox:latest"
"""
        doc_path = _write_yaml(tmp_path / "minimal.yaml", yaml_content)
        doc = load_environment_document(doc_path)
        profile = doc.profiles["minimal"]
        assert profile.docker is not None
        assert profile.docker.image == "busybox:latest"
        assert profile.docker.platform is None
        assert profile.docker.network is None
        assert profile.docker.user is None
        assert profile.docker.init is False
        assert profile.docker.read_only is False
        assert profile.docker.cap_drop_all is False
        assert profile.docker.no_new_privileges is False
        assert profile.docker.tmpfs is False
        assert profile.docker.resources.cpu is None
        assert profile.docker.resources.memory is None
        assert profile.docker.resources.pids is None

    def test_permissive_defaults_accepted(self, tmp_path: Path) -> None:
        # Requirement: permissive defaults (tag image, omitted limits, root user) accepted.
        yaml_content = """
default: dev
profiles:
  dev:
    backend: docker
    docker:
      image: "alpine:3.18"
      network: "host"
      user: "0:0"
      read_only: true
      tmpfs: false
"""
        doc_path = _write_yaml(tmp_path / "dev.yaml", yaml_content)
        doc = load_environment_document(doc_path)
        profile = doc.profiles["dev"]
        assert profile.docker is not None
        assert profile.docker.image == "alpine:3.18"
        assert profile.docker.network == "host"
        assert profile.docker.user == "0:0"
        assert profile.docker.read_only is True
        assert profile.docker.tmpfs is False

    def test_extra_key_rejected(self, tmp_path: Path) -> None:
        # Requirement: extra keys in YAML under docker or resources raise ConfigurationError.
        yaml_content = """
default: bad
profiles:
  bad:
    backend: docker
    docker:
      image: "busybox"
      unknown_option: true
"""
        doc_path = _write_yaml(tmp_path / "bad.yaml", yaml_content)
        with pytest.raises(ConfigurationError):
            load_environment_document(doc_path)


class TestDigestCompatibility:
    """Requirements for digest stability and byte-identity."""

    def test_builtin_local_environment_digest_byte_identical(self) -> None:
        # Requirement: builtin local environment digest remains byte-identical to pinned hex.
        resolved = builtin_local_environment()
        assert (
            resolved.digest
            == "sha256:778052725263d9f7227b71c3ed3a8e29608250b8c01d7966e4d08928b743c224"
        )

    def test_local_environment_model_dump_excludes_none_docker(self) -> None:
        # Requirement: model_dump of ProfileDefinition/EnvironmentDocument pops docker when None.
        profile = ProfileDefinition(backend="local")
        dump = profile.model_dump(mode="json")
        assert "docker" not in dump

        doc = EnvironmentDocument(default="default", profiles={"default": profile})
        doc_dump = doc.model_dump(mode="json")
        assert "docker" not in doc_dump["profiles"]["default"]

    def test_docker_environment_changes_digest(self) -> None:
        # Requirement: explicit docker configuration produces distinct and sensitive digests.
        doc1 = EnvironmentDocument(
            default="d",
            profiles={
                "d": ProfileDefinition(
                    backend="docker", docker=DockerProfileOptions(image="busybox:1.0")
                )
            },
        )
        doc2 = EnvironmentDocument(
            default="d",
            profiles={
                "d": ProfileDefinition(
                    backend="docker", docker=DockerProfileOptions(image="busybox:2.0")
                )
            },
        )
        doc_local = EnvironmentDocument(
            default="d",
            profiles={"d": ProfileDefinition(backend="local")},
        )

        from conductor.config.environment import _document_digest

        digest1 = _document_digest(doc1)
        digest2 = _document_digest(doc2)
        digest_local = _document_digest(doc_local)

        assert digest1 != digest2
        assert digest1 != digest_local
        assert digest2 != digest_local
