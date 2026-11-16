"""Shared hermetic Docker CLI realm and real YAML secret-delivery setup."""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from conductor.config.environment import EnvironmentDocument, ResolvedEnvironment
from conductor.config.loader import load_config
from conductor.config.schema import MCPServerDef, WorkflowConfig
from conductor.engine.secrets import SecretValueCache, index_config
from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.types import BundleRef, ResolvedExecutionSpec, RunSpec
from conductor.redaction import RunRedactor

_PEER: Final = Path(__file__).with_name("docker_stdio.py")
_CANARY: Final = "payload-only-canary-4e92"


@dataclass(frozen=True, slots=True)
class _Realm:
    backend: DockerRunnerBackend
    operator: dict[str, str]
    root: Path
    run: RunSpec


def create_realm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Realm:
    binary = tmp_path / ("docker.cmd" if sys.platform == "win32" else "docker")
    if sys.platform == "win32":
        binary.write_text(f'@"{sys.executable}" "{_PEER}" %*\n', encoding="utf-8")
    else:
        binary.write_text(
            f"#!{sys.executable}\nimport runpy\n"
            f"runpy.run_path({str(_PEER)!r}, run_name='__main__')\n",
            encoding="utf-8",
        )
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    # Keep the complete operator context synthetic, including platform launch necessities.
    platform_env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR"}
    }
    if sys.platform == "win32":
        # Requirement: snapshot CMD's default prompt without replacing an operator's value.
        platform_env["PROMPT"] = os.environ.get("PROMPT", "$P$G")
    for key in tuple(os.environ):
        monkeypatch.delenv(key)
    operator = {
        **platform_env,
        "PATH": str(tmp_path),
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "LC_CTYPE": "C.UTF-8",
        "DOCKER_HOST": "ssh://operator.invalid",
        "SSH_AUTH_SOCK": str(tmp_path / "ssh-agent"),
        "HTTPS_PROXY": "http://proxy.invalid:3128",
        "NO_PROXY": "localhost",
        "DOCKER_CONFIG": str(tmp_path / "docker-config"),
        "CREDENTIAL_HELPER_CONTEXT": "operator-helper",
        "AMBIENT_SOURCE": "ambient-source-allowed-2178",
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.jsonl"),
        "FAKE_DOCKER_STATE": str(tmp_path / "state.json"),
        "MCP_PEER_RECEIPT": str(tmp_path / "peer.json"),
    }
    for key, value in operator.items():
        monkeypatch.setenv(key, value)
    backend = DockerRunnerBackend(str(binary))
    operator = dict(os.environ)
    tree = tmp_path / "bundle" / "tree" / "main"
    tree.mkdir(parents=True)
    run = RunSpec(
        "sdk-composition",
        bundle=BundleRef("sha256:composition", str(tree.parent.parent)),
        mcp_fence_execution=ResolvedExecutionSpec("peer:image"),
    )
    # These sources resolve later; they must not be overlaid onto the captured CLI context.
    monkeypatch.setenv("LATE_SOURCE", _CANARY)
    monkeypatch.setenv("AGENT_SOURCE", "agent-only-value-3141")
    return _Realm(backend, operator, tmp_path, run)


def _config(root: Path, delivery: str, ref: str = "ambient") -> WorkflowConfig:
    path = root / "workflow.yaml"
    path.write_text(
        f"""workflow:
  name: composition
  entry_point: done
  runtime:
    mcp_servers:
      srv:
        command: peer
        env:
          LITERAL: authored
          DEFAULT: ${{UNSET_COMPOSITION:-fallback}}
          EXPANDED: ${{AMBIENT_SOURCE}}
          LC_CTYPE: C.UTF-8
        secrets:
          - {{ref: {ref}, scope: mcp, delivery: {{env: {delivery}}}}}
          - {{ref: payload, scope: mcp, delivery: {{env: PAYLOAD_TOKEN}}}}
          - {{ref: agent_only, scope: agent, delivery: {{env: AGENT_ONLY}}}}
agents:
  - name: done
    type: set
    value: "'ok'"
    routes: [{{to: $end}}]
""",
        encoding="utf-8",
    )
    return load_config(path)


async def _resolve(config: WorkflowConfig) -> MCPServerDef:
    document = EnvironmentDocument.model_validate(
        {
            "default": "local",
            "profiles": {"local": {"backend": "local"}},
            "secrets": {
                name: {"source": {"env": source}, "allow": [scope]}
                for name, source, scope in (
                    ("ambient", "AMBIENT_SOURCE", "mcp"),
                    ("payload", "LATE_SOURCE", "mcp"),
                    ("agent_only", "AGENT_SOURCE", "agent"),
                )
            },
        }
    )
    environment = ResolvedEnvironment(
        document=document, name="composition", source="path", path=None, digest="sha256:test"
    )
    uses = index_config(config, SecretValueCache(environment, RunRedactor()))
    from conductor.mcp_auth import resolve_mcp_server_config

    return MCPServerDef.model_validate(
        await resolve_mcp_server_config(
            "srv", config.workflow.runtime.mcp_servers["srv"].model_dump(), secret_uses=uses
        )
    )
