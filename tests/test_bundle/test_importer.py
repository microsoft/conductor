"""Tests for the OCI archive and existing CAS trust boundaries."""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import shutil
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conductor.bundle.importer import BundleImportError, import_bundle, verify_bundle
from conductor.bundle.model import (
    BundleEntry,
    BundleManifest,
    compute_bundle_digest,
    serialize_manifest,
)
from conductor.bundle.oci import OciConfig, encode_oci_config, parse_oci_config
from conductor.bundle.store import bundle_store_base, bundle_store_path, publish_bundle

_ROOT = "tree/project/flow.yaml"
_PAYLOAD = b"not loaded as a workflow\n"


def _file(path: str = _ROOT) -> BundleEntry:
    return BundleEntry(
        logical_path=path,
        kind="file",
        size=len(_PAYLOAD),
        executable=path == _ROOT,
        digest=f"sha256:{hashlib.sha256(_PAYLOAD).hexdigest()}",
        link_target=None,
        origin_kind="workflow" if path == _ROOT else "asset",
        origin_detail="workflow:root" if path == _ROOT else path,
    )


def _link(path: str, link: str) -> BundleEntry:
    return BundleEntry.for_symlink(path, link, origin_kind="asset", origin_detail=path)


def _config(*extra: BundleEntry) -> OciConfig:
    entries = tuple(sorted((_file(), *extra), key=lambda entry: entry.logical_path))
    manifest = BundleManifest(
        version=1,
        bundle_digest=compute_bundle_digest(entries, {}, {}),
        entries=entries,
        skills_topology={},
        plugins_topology={},
    )
    return parse_oci_config(encode_oci_config(manifest))


def _layer(path: Path, config: OciConfig, *, fault: str = "") -> Path:
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w", format=tarfile.PAX_FORMAT) as tar:
        manifest = config.manifest.model_copy(update={"skills_topology": {"wrong": _ROOT}})
        raw = serialize_manifest(manifest if fault == "manifest" else config.manifest).encode()
        info = tarfile.TarInfo(".bundle/manifest.json")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
        for entry in config.manifest.entries:
            member = tarfile.TarInfo(entry.logical_path)
            if entry.link_target is not None:
                member.type = tarfile.SYMTYPE
                member.linkname = entry.link_target
                tar.addfile(member)
                continue
            payload = _PAYLOAD
            member.mode = 0o755 if entry.executable else 0o644
            member.size = len(payload)
            if entry.logical_path == _ROOT:
                if fault == "pax":
                    member.name = "header-name"
                    member.pax_headers = {"path": entry.logical_path}
                if fault == "mode":
                    member.mode = 0o644
                if fault == "digest":
                    payload = bytes(len(payload))
                if fault == "size":
                    payload += b"x"
                    member.size += 1
                if fault == "hardlink":
                    member.type = tarfile.LNKTYPE
                    member.size = 0
                    member.linkname = "canary"
                if fault == "device":
                    member.type = tarfile.CHRTYPE
                    member.size = 0
                if fault == "sparse":
                    member.pax_headers = {"GNU.sparse.realsize": str(member.size)}
            tar.addfile(member, io.BytesIO(payload))
        if fault in {"duplicate", "../canary", "/canary"}:
            tar.addfile(tarfile.TarInfo(_ROOT if fault == "duplicate" else fault))
        end = tar.offset
    raw_tar = tar_bytes.getvalue()
    if fault == "tail":
        raw_tar = raw_tar[: end + 512] + b"X" + raw_tar[end + 513 :]
    if fault == "padding":
        raw_tar = raw_tar[: end + 1024] + b"X" + raw_tar[end + 1025 :]
    path.write_bytes(gzip.compress(raw_tar, mtime=123))
    return path


def test_imports_effective_pax_paths_and_publishes_ordinary_archive(tmp_path: Path) -> None:
    # Requirement: valid noncanonical wire bytes are accepted, but CAS uses the ordinary writer.
    config = _config()
    layer = _layer(tmp_path / "layer.gz", config, fault="pax")
    final = import_bundle(layer, config)

    assert final == bundle_store_path(config.bundle_digest)
    assert (final / _ROOT).read_bytes() == _PAYLOAD
    assert verify_bundle(config.bundle_digest) == config.manifest
    assert (final / "bundle.tar.gz").read_bytes() != layer.read_bytes()


def test_import_materializes_contained_directory_link(tmp_path: Path) -> None:
    # Requirement: shared directory targets are valid, and file aliases stay file links.
    probe = tmp_path / "probe"
    try:
        probe.symlink_to(".", target_is_directory=True)
    except OSError as exc:
        if sys.platform == "win32" and exc.winerror == 1314:
            pytest.skip("Windows symlink creation privilege is unavailable")
        raise
    link = "tree/other"
    config = _config(
        _link(link, "project"),
        _link("tree/shared", "other"),
        _link("tree/file-alias", "shared/flow.yaml"),
    )

    final = import_bundle(_layer(tmp_path / "layer.gz", config), config)

    assert (final / link).is_symlink()
    assert (final / link / "flow.yaml").read_bytes() == _PAYLOAD
    assert (final / "tree/shared/flow.yaml").read_bytes() == _PAYLOAD
    assert (final / "tree/file-alias").is_symlink()
    assert (final / "tree/file-alias").read_bytes() == _PAYLOAD


@pytest.mark.parametrize(
    "fault",
    [
        "bundle.json",
        "tree/project/file:stream",
        "tree/project/NUL",
        "tree/project/CoM¹ .txt",
        "tree/PROJECT/other",
        "tree/project/flow.yaml/child",
        "empty-directory-link",
        "escape-link",
        "cycle-link",
        "directory-self-cycle",
        "directory-ancestor-cycle",
        "directory-composed-cycle",
        "link-through-file",
        "nonportable-link",
        "duplicate",
        "../canary",
        "/canary",
        "manifest",
        "mode",
        "digest",
        "size",
        "hardlink",
        "device",
        "sparse",
        "tail",
        "padding",
    ],
)
def test_invalid_layer_does_not_publish_or_touch_canary(tmp_path: Path, fault: str) -> None:
    # Requirement: invalid namespace, content, or terminator fails before CAS publication.
    canary = tmp_path / "canary"
    canary.write_bytes(b"external")
    targets = {
        "empty-directory-link": "empty",
        "escape-link": "../../canary",
        "cycle-link": "link",
        "directory-self-cycle": ".",
        "directory-ancestor-cycle": "..",
        "link-through-file": "flow.yaml/../flow.yaml",
        "nonportable-link": "NUL",
    }
    if fault in targets:
        config = _config(_link("tree/project/link", targets[fault]))
    elif fault == "directory-composed-cycle":
        config = _config(
            _link("tree/project/sub/to-other", "../../other"),
            _link("tree/other/to-project", "../project"),
        )
    elif fault.startswith("tree/") or fault == "bundle.json":
        config = _config(_file(fault))
    else:
        config = _config()
    layer = _layer(tmp_path / "layer.gz", config, fault=fault)

    with pytest.raises(BundleImportError):
        import_bundle(layer, config)

    assert not bundle_store_base().exists()
    assert canary.read_bytes() == b"external"


@pytest.mark.parametrize("node", [_ROOT, ".bundle/manifest.json", "bundle.tar.gz"])
def test_refuses_seeded_cas_with_valid_sentinel_without_repair(tmp_path: Path, node: str) -> None:
    # Requirement: both online reuse and offline verify refuse stale verified-looking CAS data.
    config = _config()
    layer = _layer(tmp_path / "layer.gz", config)
    final = publish_bundle(config.manifest, {_ROOT: _PAYLOAD}, {})
    sentinel = (final / "bundle.json").read_bytes()
    broken = final / node
    broken.write_bytes(b"bad")

    with pytest.raises(BundleImportError):
        import_bundle(layer, config)
    with pytest.raises(BundleImportError):
        verify_bundle(config.bundle_digest)

    assert broken.read_bytes() == b"bad"
    assert (final / "bundle.json").read_bytes() == sentinel


@pytest.mark.parametrize(
    "placement", ["lock-hardlink", "lock-symlink", "cache-symlink", "sentinel"]
)
def test_store_never_follows_unexpected_redirections(tmp_path: Path, placement: str) -> None:
    # Requirement: malicious namespace or lock redirections cannot touch an external canary.
    config = _config()
    layer = _layer(tmp_path / "layer.gz", config)
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary"
    canary.write_bytes(b"external")
    base = bundle_store_base()
    lock = base / f".{bundle_store_path(config.bundle_digest).name}.lock"
    if placement == "lock-hardlink":
        base.mkdir(parents=True)
        os.link(canary, lock)
    else:
        targets = {
            "lock-symlink": (lock, canary),
            "cache-symlink": (base.parent, outside),
            "sentinel": (bundle_store_path(config.bundle_digest) / "bundle.json", canary),
        }
        path, target = targets[placement]
        path.parent.mkdir(parents=True)
        try:
            path.symlink_to(target, target_is_directory=target == outside)
        except OSError as exc:
            if sys.platform == "win32" and exc.winerror == 1314:
                pytest.skip("Windows symlink creation privilege is unavailable")
            raise

    with pytest.raises(BundleImportError):
        import_bundle(layer, config)

    assert list(outside.iterdir()) == [canary]
    assert canary.read_bytes() == b"external"
    assert not (bundle_store_path(config.bundle_digest) / _ROOT).exists()


def test_concurrent_importers_return_same_verified_cas(tmp_path: Path) -> None:
    # Requirement: concurrent imports of the same B share one fully verified publication.
    config = _config()
    layer = _layer(tmp_path / "layer.gz", config)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(import_bundle, layer, config) for _ in range(2)]
        first, second = (future.result(timeout=15) for future in futures)

    assert first == second == bundle_store_path(config.bundle_digest)
    assert verify_bundle(config.bundle_digest) == config.manifest
    assert sorted(path.name for path in first.parent.iterdir()) == [
        f".{first.name}.lock",
        first.name,
    ]


def test_import_does_not_accept_corrupt_rename_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: a lost rename race verifies the actual winner, not the losing staging tree.
    config = _config()
    layer = _layer(tmp_path / "layer.gz", config)
    final = bundle_store_path(config.bundle_digest)
    real_replace = os.replace

    def replace(source: Path, target: Path) -> None:
        if target == final:
            shutil.copytree(source, target)
            (target / _ROOT).write_bytes(b"bad")
        real_replace(source, target)

    monkeypatch.setattr("conductor.bundle.store.os.replace", replace)
    with pytest.raises(BundleImportError):
        import_bundle(layer, config)

    assert (final / _ROOT).read_bytes() == b"bad"
    assert sorted(path.name for path in final.parent.iterdir()) == [
        f".{final.name}.lock",
        final.name,
    ]
