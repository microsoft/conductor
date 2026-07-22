"""Integration coverage for ``scripts/aca/provision-pool.sh`` (epic E5).

The script drives the Azure CLI end-to-end and cannot be exercised against
real Azure in CI/tests, so these tests run it against a scripted mock ``az``
(``_mock_az.py``) that records every invocation's argv and returns just
enough canned ``--query`` output to keep the script's control flow moving.
Assertions inspect the recorded argv to verify the previously-unverified
behaviors flagged in review:

- ``TARGET_PORT`` propagation into ``az acr build --build-arg``.
- ``--cooldown-period`` / ``--max-alive-period`` mutual exclusivity per
  ``LIFECYCLE``.
- The registry role-assignment grant uses ``--assignee-object-id`` /
  ``--assignee-principal-type ServicePrincipal`` (not the graph-lookup-based
  ``--assignee``, which races Entra ID replication for a just-created
  identity).
- ABAC-vs-legacy registry role selection (``Container Registry Repository
  Reader`` vs ``AcrPull``).
- The default ``IMAGE_TAG`` is unique across runs (not ``latest``), and an
  explicit override is honored.
- The az CLI / ``containerapp`` extension preflight (``az upgrade --yes``,
  ``az extension add --name containerapp --upgrade --allow-preview true
  --yes``) runs before any session-pool-specific calls.
- Invalid ``EGRESS``/``LIFECYCLE`` values are rejected *before* the preflight
  or any other Azure CLI call is made (the mock ``az`` log stays empty).
- An empty/unrecognized ACR ``roleAssignmentMode`` falls back to the legacy
  ``AcrPull`` role.
- The Session Executor role grant (on the pool, for a human/CI principal)
  uses the simple ``--assignee`` form — unlike the registry grant, it is not
  racing a just-created identity's Entra ID replication, so it doesn't need
  ``--assignee-object-id``/``--assignee-principal-type``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "aca" / "provision-pool.sh"
MOCK_AZ_SRC = Path(__file__).resolve().parent / "_mock_az.py"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="provision-pool.sh is a bash script"
)


def _install_mock_az(bin_dir: Path) -> None:
    """Copy the mock ``az`` shim into ``bin_dir`` and make it executable."""
    az_path = bin_dir / "az"
    shutil.copyfile(MOCK_AZ_SRC, az_path)
    az_path.chmod(az_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def run_provision_script_raw(
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Run ``provision-pool.sh`` against the mock ``az`` without asserting success.

    Returns the completed process (whatever its exit code) alongside the argv
    log of every ``az`` invocation actually made, in call order. Used both by
    ``run_provision_script`` (the happy-path helper) and by tests that expect
    the script to fail validation before making any Azure CLI call.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    _install_mock_az(bin_dir)

    log_path = tmp_path / "az_calls.jsonl"

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["MOCK_AZ_LOG"] = str(log_path)
    env.setdefault("RESOURCE_GROUP", "mock-rg")
    env.setdefault("CONTAINERAPP_ENVIRONMENT", "mock-env")
    env.setdefault("ACR_NAME", "mockacr")
    if extra_env:
        env.update(extra_env)

    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    if not log_path.exists():
        calls: list[list[str]] = []
    else:
        calls = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    return proc, calls


def run_provision_script(
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
) -> list[list[str]]:
    """Run ``provision-pool.sh`` against the mock ``az`` and return its argv log.

    Returns a list of argv lists, one per ``az`` invocation, in call order.
    Asserts the script exits successfully; use ``run_provision_script_raw``
    directly for the failure paths (e.g. invalid ``EGRESS``/``LIFECYCLE``).
    """
    proc, calls = run_provision_script_raw(tmp_path, extra_env)
    assert proc.returncode == 0, (
        f"provision-pool.sh failed (exit {proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    return calls


def _find_calls(calls: list[list[str]], *prefix: str) -> list[list[str]]:
    """Return every call whose leading args match ``prefix``."""
    return [c for c in calls if c[: len(prefix)] == list(prefix)]


def _flag_value(call: list[str], flag: str) -> str | None:
    """Return the value following ``flag`` in ``call``, if present."""
    if flag in call:
        idx = call.index(flag)
        if idx + 1 < len(call):
            return call[idx + 1]
    return None


class TestTargetPortPropagation:
    def test_target_port_forwarded_to_acr_build(self, tmp_path: Path) -> None:
        calls = run_provision_script(tmp_path, {"TARGET_PORT": "9090"})

        build_calls = _find_calls(calls, "acr", "build")
        assert len(build_calls) == 1
        assert f"TARGET_PORT=9090" in build_calls[0]  # noqa: F541 - explicit for clarity

        pool_create_calls = _find_calls(calls, "containerapp", "sessionpool", "create")
        assert len(pool_create_calls) == 1
        assert _flag_value(pool_create_calls[0], "--target-port") == "9090"


class TestLifecycleFlagExclusivity:
    def test_timed_lifecycle_uses_cooldown_only(self, tmp_path: Path) -> None:
        calls = run_provision_script(tmp_path, {"LIFECYCLE": "timed"})
        pool_create = _find_calls(calls, "containerapp", "sessionpool", "create")[0]

        assert _flag_value(pool_create, "--lifecycle-type") == "Timed"
        assert "--cooldown-period" in pool_create
        assert "--max-alive-period" not in pool_create

    def test_on_container_exit_lifecycle_uses_max_alive_only(self, tmp_path: Path) -> None:
        calls = run_provision_script(tmp_path, {"LIFECYCLE": "on_container_exit"})
        pool_create = _find_calls(calls, "containerapp", "sessionpool", "create")[0]

        assert _flag_value(pool_create, "--lifecycle-type") == "OnContainerExit"
        assert "--max-alive-period" in pool_create
        assert "--cooldown-period" not in pool_create

    def test_invalid_lifecycle_rejected_before_any_az_call(self, tmp_path: Path) -> None:
        proc, calls = run_provision_script_raw(tmp_path, {"LIFECYCLE": "bogus"})

        assert proc.returncode != 0
        # Validation must reject the bad value before the preflight (or any
        # other) Azure CLI call is made — otherwise a bogus config still
        # burns an `az upgrade`/`az extension add` round-trip before failing.
        assert calls == []


class TestEgressValidation:
    def test_invalid_egress_rejected_before_any_az_call(self, tmp_path: Path) -> None:
        proc, calls = run_provision_script_raw(tmp_path, {"EGRESS": "bogus"})

        assert proc.returncode != 0
        assert calls == []


class TestRegistryRoleAssignment:
    def test_uses_object_id_and_principal_type_not_assignee(self, tmp_path: Path) -> None:
        calls = run_provision_script(tmp_path)

        role_calls = _find_calls(calls, "role", "assignment", "create")
        # The first role assignment is the registry pull grant, the second
        # is the Session Executor grant on the pool.
        registry_role_call = role_calls[0]

        assert _flag_value(registry_role_call, "--assignee-object-id") == (
            "11111111-1111-1111-1111-111111111111"
        )
        assert _flag_value(registry_role_call, "--assignee-principal-type") == "ServicePrincipal"
        # The graph-lookup-based flag must not be used for the just-created
        # identity (Entra ID replication race).
        assert "--assignee" not in registry_role_call

    def test_legacy_registry_uses_acrpull_role(self, tmp_path: Path) -> None:
        calls = run_provision_script(
            tmp_path, {"MOCK_ACR_ROLE_ASSIGNMENT_MODE": "LegacyRegistryPermissions"}
        )
        role_calls = _find_calls(calls, "role", "assignment", "create")
        assert _flag_value(role_calls[0], "--role") == "AcrPull"

    def test_abac_registry_uses_repository_reader_role(self, tmp_path: Path) -> None:
        calls = run_provision_script(
            tmp_path, {"MOCK_ACR_ROLE_ASSIGNMENT_MODE": "AbacRepositoryPermissions"}
        )
        role_calls = _find_calls(calls, "role", "assignment", "create")
        assert _flag_value(role_calls[0], "--role") == "Container Registry Repository Reader"

    def test_unrecognized_role_assignment_mode_falls_back_to_acrpull(self, tmp_path: Path) -> None:
        # An empty or unrecognized `roleAssignmentMode` (e.g. an older `az
        # acr` API version that predates ABAC) must fall back to the legacy
        # `AcrPull` role rather than erroring or silently granting nothing.
        calls = run_provision_script(
            tmp_path, {"MOCK_ACR_ROLE_ASSIGNMENT_MODE": "SomeFutureUnknownMode"}
        )
        role_calls = _find_calls(calls, "role", "assignment", "create")
        assert _flag_value(role_calls[0], "--role") == "AcrPull"

    def test_empty_role_assignment_mode_falls_back_to_acrpull(self, tmp_path: Path) -> None:
        calls = run_provision_script(tmp_path, {"MOCK_ACR_ROLE_ASSIGNMENT_MODE": ""})
        role_calls = _find_calls(calls, "role", "assignment", "create")
        assert _flag_value(role_calls[0], "--role") == "AcrPull"


class TestSessionExecutorRoleAssignment:
    def test_uses_plain_assignee_not_object_id(self, tmp_path: Path) -> None:
        # Unlike the registry grant (which targets a just-created identity
        # and must avoid the Entra ID replication race), the Session
        # Executor grant targets an already-existing principal (the signed-in
        # user, or an explicit $ASSIGNEE) — the simple `--assignee` graph
        # lookup is fine here.
        calls = run_provision_script(tmp_path, {"ASSIGNEE": "33333333-3333-3333-3333-333333333333"})

        role_calls = _find_calls(calls, "role", "assignment", "create")
        assert len(role_calls) == 2
        session_executor_call = role_calls[1]

        assert _flag_value(session_executor_call, "--role") == (
            "Azure ContainerApps Session Executor"
        )
        assert _flag_value(session_executor_call, "--assignee") == (
            "33333333-3333-3333-3333-333333333333"
        )
        assert "--assignee-object-id" not in session_executor_call
        assert "--assignee-principal-type" not in session_executor_call

    def test_defaults_assignee_to_signed_in_user(self, tmp_path: Path) -> None:
        calls = run_provision_script(tmp_path)

        signed_in_user_calls = _find_calls(calls, "ad", "signed-in-user", "show")
        assert len(signed_in_user_calls) == 1

        role_calls = _find_calls(calls, "role", "assignment", "create")
        session_executor_call = role_calls[1]
        assert _flag_value(session_executor_call, "--assignee") == (
            "22222222-2222-2222-2222-222222222222"
        )


class TestImageTag:
    def test_default_tag_is_unique_across_runs(self, tmp_path: Path) -> None:
        calls_a = run_provision_script(tmp_path / "a")
        calls_b = run_provision_script(tmp_path / "b")

        build_a = _find_calls(calls_a, "acr", "build")[0]
        build_b = _find_calls(calls_b, "acr", "build")[0]
        image_a = _flag_value(build_a, "--image")
        image_b = _flag_value(build_b, "--image")

        assert image_a is not None and ":latest" not in image_a
        assert image_b is not None and ":latest" not in image_b
        # The default tag must be collision-resistant (not just second-
        # resolution wall-clock time), so two runs never collide even when
        # they land in the same wall-clock second.
        assert image_a != image_b

    def test_explicit_image_tag_is_honored(self, tmp_path: Path) -> None:
        calls = run_provision_script(tmp_path, {"IMAGE_TAG": "v1.2.3"})
        build_call = _find_calls(calls, "acr", "build")[0]
        assert _flag_value(build_call, "--image") == "conductor-agent-runner:v1.2.3"


class TestPreflight:
    def test_az_upgrade_and_extension_upgrade_run_before_sessionpool_calls(
        self, tmp_path: Path
    ) -> None:
        calls = run_provision_script(tmp_path)

        upgrade_calls = _find_calls(calls, "upgrade")
        extension_calls = _find_calls(calls, "extension", "add")
        assert len(upgrade_calls) == 1
        assert len(extension_calls) == 1
        assert "--yes" in upgrade_calls[0]
        assert _flag_value(extension_calls[0], "--name") == "containerapp"
        assert "--upgrade" in extension_calls[0]
        # `--allow-preview true` and `--yes` are both required for a
        # non-interactive install of the (at time of writing) preview
        # `containerapp` extension — either missing would hang/fail in CI.
        assert _flag_value(extension_calls[0], "--allow-preview") == "true"
        assert "--yes" in extension_calls[0]

        first_sessionpool_idx = next(
            i for i, c in enumerate(calls) if c[:2] == ["containerapp", "sessionpool"]
        )
        upgrade_idx = calls.index(upgrade_calls[0])
        extension_idx = calls.index(extension_calls[0])
        assert upgrade_idx < first_sessionpool_idx
        assert extension_idx < first_sessionpool_idx
