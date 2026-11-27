"""Hermetic OCI CLI tests at the registry adapter boundary."""

import re
import sys
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tarfile import open as open_tar
from typing import NoReturn

import pytest
from typer.testing import CliRunner

from conductor.bundle import oci_receipts, oci_remote
from conductor.bundle.model import BundleManifest
from conductor.bundle.oci import OciReference, encode_oci_config, parse_oci_config
from conductor.bundle.store import bundle_store_path
from conductor.cli.app import app

_TAG = "oci://registry.example/team/bundle:Live"
_REPO = _TAG.removesuffix(":Live")
_WORKFLOW = """\
workflow:
  name: oci-smoke
  entry_point: mark
agents:
  - name: mark
    type: set
    value: "42"
    routes:
      - to: $end
"""
_ENTRYPOINT = "tree/main/work[flow].yaml"
_M1 = "sha256:" + "1" * 64
_M2 = "sha256:" + "2" * 64
runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HOME", "USERPROFILE", "DOCKER_CONFIG"):
        monkeypatch.setenv(name, str(tmp_path / name))

    def unexpected(
        *args: str | urllib.request.Request | oci_remote.OciRemote | OciReference,
        **kwargs: float,
    ) -> NoReturn:
        pytest.fail(f"Unexpected real network/SDK access: {args!r}, {kwargs!r}")

    monkeypatch.setattr("urllib.request.urlopen", unexpected)
    monkeypatch.setattr(oci_remote.OciRemote, "__init__", unexpected)


@pytest.fixture
def workflow(tmp_path: Path) -> Path:
    path = tmp_path / "work[flow].yaml"
    path.write_text(_WORKFLOW, encoding="utf-8")
    return path


def test_push_uses_fresh_verified_archive(workflow: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: push uploads freshly collected bytes, after the CAS is ready.
    def upload(self: oci_remote.OciRemote, manifest: BundleManifest, layer: Path) -> str:
        assert layer != bundle_store_path(manifest.bundle_digest) / "bundle.tar.gz"
        assert (bundle_store_path(manifest.bundle_digest) / _ENTRYPOINT).read_bytes() == (
            workflow.read_bytes()
        )
        with open_tar(layer, "r:gz") as archive:
            assert (member := archive.extractfile(_ENTRYPOINT)) is not None
            assert member.read() == workflow.read_bytes()
        return f"sha256:{sha256(layer.read_bytes()).hexdigest()}"

    def setup(self: oci_remote.OciRemote, ref: OciReference, auth: oci_remote.AuthBackend) -> None:
        assert str(ref) == _TAG
        assert auth == "basic"

    monkeypatch.setattr(oci_remote.OciRemote, "__init__", setup)
    monkeypatch.setattr(oci_remote.OciRemote, "push", upload)
    pushed = runner.invoke(app, ["bundle", "push", str(workflow), "--to", _TAG, "--auth", "basic"])
    assert pushed.exit_code == 0, pushed.output
    resolved = runner.invoke(app, ["bundle", "pull", _TAG, "--offline"])
    assert resolved.exit_code == 0, resolved.output
    assert resolved.stdout == pushed.stdout
    assert f"Entrypoint: {_ENTRYPOINT}" in resolved.stdout


def test_push_refuses_a_corrupt_local_cas_before_registry_setup(
    workflow: Path,
) -> None:
    # Requirement: a parse-only cache hit cannot bypass pre-upload verification.
    built = runner.invoke(app, ["bundle", "build", str(workflow)])
    assert built.exit_code == 0, built.output
    match = re.search(r"sha256:[0-9a-f]{64}", "".join(built.stdout.split()))
    assert match is not None
    stored_workflow = bundle_store_path(match.group()) / _ENTRYPOINT
    stored_workflow.write_text("corrupt", encoding="utf-8")

    pushed = runner.invoke(app, ["bundle", "push", str(workflow), "--to", _TAG])

    assert pushed.exit_code == 1, pushed.output
    assert "mismatch" in pushed.stderr
    assert stored_workflow.read_text(encoding="utf-8") == "corrupt"


@pytest.mark.parametrize(
    ("args", "diagnostic"),
    [
        (["pull", "oci://registry.example/team/bundle", "--offline"], "Expected oci://"),
        (["push", "missing.yaml", "--to", f"{_REPO}@{_M1}"], "requires a tag"),
    ],
)
def test_cli_rejects_invalid_selectors_before_work(args: list[str], diagnostic: str) -> None:
    # Requirement: selectors are explicit, and push accepts only a mutable tag.
    result = runner.invoke(app, ["bundle", *args])

    assert result.exit_code == 1
    assert diagnostic in result.stderr


def test_cold_terminal_offline_miss_does_not_check_for_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: offline fails with a recovery instruction even in a cold terminal.
    from conductor.cli.app import console

    args = ["--quiet", "bundle", "pull", _TAG, "--offline", "--auth", "basic"]
    monkeypatch.setattr(sys, "argv", ["conductor", *args])
    monkeypatch.setattr(console, "_force_terminal", True)
    monkeypatch.delenv("CONDUCTOR_NO_UPDATE_CHECK", raising=False)

    result = runner.invoke(app, args)

    assert result.exit_code == 1, result.output
    assert not result.stdout
    assert _TAG in result.stderr
    assert "without --offline" in result.stderr


def test_terminal_bundle_build_still_checks_for_updates(
    workflow: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: the offline exception does not disable normal bundle startup hints.
    from conductor.cli import update
    from conductor.cli.app import console

    args = ["bundle", "build", str(workflow)]
    seen: list[bool] = []
    monkeypatch.setattr(sys, "argv", ["conductor", *args])
    monkeypatch.setattr(console, "_force_terminal", True)
    monkeypatch.setattr(
        update, "check_for_update_hint", lambda target: seen.append(target.is_terminal)
    )

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert seen == [True]


def test_failed_online_pull_never_falls_back_and_pointer_failure_keeps_both_digests(
    workflow: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: a failed transfer does not use stale data; failed pointer replace
    # leaves the old tag and the new immutable receipt available independently.
    from conductor.bundle.collector import collect_bundle

    monkeypatch.setenv("CONDUCTOR_HOME", str(workflow.parent / "[cas]"))
    built = runner.invoke(app, ["bundle", "build", str(workflow)])
    assert built.exit_code == 0, built.output
    collected = collect_bundle(
        workflow, environment=None, allow_network=False, on_warning=pytest.fail
    )
    config = parse_oci_config(encode_oci_config(collected.manifest))
    layer = bundle_store_path(config.bundle_digest) / "bundle.tar.gz"

    @contextmanager
    def download(self: oci_remote.OciRemote) -> Iterator[oci_remote.PulledBundle]:
        if digest is None:
            raise oci_remote.OciRemoteError("Registry unavailable")
        yield oci_remote.PulledBundle(digest, config, layer)

    monkeypatch.setattr(oci_remote.OciRemote, "__init__", lambda self, ref, auth: None)
    monkeypatch.setattr(oci_remote.OciRemote, "pull", download)
    digest: str | None = _M1
    args = ["bundle", "pull", _TAG]
    authored = _TAG.replace("registry.example", "REGISTRY.example:443")
    first = runner.invoke(app, ["bundle", "pull", authored])
    assert first.exit_code == 0, first.output
    assert f"{_REPO}@{_M1}" in first.stdout
    # Requirement: only online pull prints the literal path to the verified CAS.
    assert first.stdout.splitlines()[-1] == f"Store: {layer.parent}"

    digest = None
    unavailable = runner.invoke(app, args)
    assert unavailable.exit_code == 1
    assert not unavailable.stdout
    assert _M1 not in unavailable.stderr

    digest = _M2
    pointer_name = f"{sha256(_TAG.encode()).hexdigest()}.json"
    replace = oci_receipts.os.replace

    def fail_pointer(source: Path, destination: Path) -> None:
        if destination.name == pointer_name:
            raise PermissionError("Injected pointer replace failure")
        replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(oci_receipts.os, "replace", fail_pointer)
        failed = runner.invoke(app, args)
    assert failed.exit_code == 1
    assert not failed.stdout
    assert f"{_REPO}@{_M2}" in failed.stderr.splitlines()

    old = runner.invoke(app, [*args, "--offline"])
    new = runner.invoke(app, ["bundle", "pull", f"{_REPO}@{_M2}", "--offline"])
    assert old.exit_code == new.exit_code == 0
    assert f"{_REPO}@{_M1}" in old.stdout
    assert f"{_REPO}@{_M2}" in new.stdout
    for result in (old, new):
        assert f"Bundle digest: {config.bundle_digest}" in result.stdout
        assert "Store:" not in result.stdout
    receipt_dir = layer.parent.parent / "oci-receipts"
    expected_refs = (_TAG, f"{_REPO}@{_M1}", f"{_REPO}@{_M2}")
    assert {path.name for path in receipt_dir.iterdir()} == {
        f"{sha256(ref.encode()).hexdigest()}.json" for ref in expected_refs
    }
