"""Tests for :mod:`conductor.bundle.store` — the content-addressed bundle store.

The store contract under test: a bundle publishes to
``<CONDUCTOR_HOME>/cache/bundles/sha256-<hex>/`` with the entry tree, a
deterministic ``bundle.tar.gz``, and ``bundle.json`` written last as the
readiness sentinel; republishing reuses a valid directory without re-hashing;
an invalid directory is warned about, removed, and rebuilt; a crash mid-write
leaves no residue; concurrent publishers of one digest both end up with a
valid store path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conductor.bundle.model import (
    BundleEntry,
    BundleManifest,
    compute_bundle_digest,
    serialize_manifest,
)
from conductor.bundle.store import (
    bundle_store_base,
    bundle_store_key,
    bundle_store_path,
    publish_bundle,
)

_POSIX = hasattr(os, "symlink")


def _file_entry(logical_path: str, data: bytes, *, executable: bool = False) -> BundleEntry:
    return BundleEntry(
        logical_path=logical_path,
        kind="file",
        digest=f"sha256:{hashlib.sha256(data).hexdigest()}",
        size=len(data),
        executable=executable,
        link_target=None,
        origin_kind="asset",
        origin_detail=f"asset:{logical_path}",
    )


def _symlink_entry(logical_path: str, target: str) -> BundleEntry:
    return BundleEntry.for_symlink(
        logical_path,
        target,
        origin_kind="asset",
        origin_detail=f"asset:{logical_path}",
    )


def _manifest(entries: list[BundleEntry]) -> BundleManifest:
    digest = compute_bundle_digest(entries, {}, {})
    return BundleManifest(
        version=1,
        bundle_digest=digest,
        entries=tuple(entries),
        skills_topology={},
        plugins_topology={},
    )


def _payload_manifest(
    with_symlink: bool = _POSIX,
) -> tuple[BundleManifest, dict[str, bytes], dict[str, str]]:
    """A consistent manifest + files + links fixture for the happy path."""
    entries = [
        _file_entry("tree/main/workflow.yaml", b"workflow: {entry_point: a}\n"),
        _file_entry("tree/main/scripts/run.sh", b"#!/bin/sh\necho hi\n", executable=True),
        _file_entry("tree/main/plain.txt", b"plain\n"),
    ]
    links: dict[str, str] = {}
    if with_symlink:
        entries.append(_symlink_entry("tree/main/link.txt", "plain.txt"))
        links = {"tree/main/link.txt": "plain.txt"}
    files = {
        "tree/main/workflow.yaml": b"workflow: {entry_point: a}\n",
        "tree/main/scripts/run.sh": b"#!/bin/sh\necho hi\n",
        "tree/main/plain.txt": b"plain\n",
    }
    return _manifest(entries), files, links


class TestBundleStoreBase:
    def test_honors_conductor_home(self):
        # Requirement: the store base resolves under $CONDUCTOR_HOME (the
        # conftest tmp-home fixture sets it), mirroring the plugin cache idiom.
        base = bundle_store_base()
        assert base == Path(os.environ["CONDUCTOR_HOME"]) / "cache" / "bundles"

    def test_digest_uses_portable_filesystem_key(self):
        # Requirement: the shared store lookup replaces the digest colon with
        # a hyphen while the manifest digest itself remains unchanged.
        digest = "sha256:" + "a" * 64

        assert bundle_store_key(digest) == "sha256-" + "a" * 64
        assert bundle_store_path(digest) == bundle_store_base() / ("sha256-" + "a" * 64)
        assert ":" not in bundle_store_path(digest).name


class TestPublish:
    def test_publish_creates_complete_store_dir(self):
        # Requirement: publishing stages the entry tree at full store-relative
        # logical paths, writes bundle.tar.gz, and writes bundle.json last as
        # the readiness sentinel whose bundle_digest equals the dir name.
        manifest, files, links = _payload_manifest()
        final = publish_bundle(manifest, files, links)

        assert final == bundle_store_path(manifest.bundle_digest)
        assert final.is_dir()
        assert (final / "tree/main/workflow.yaml").read_bytes() == files["tree/main/workflow.yaml"]
        assert (final / "bundle.tar.gz").is_file()
        parsed = json.loads((final / "bundle.json").read_text(encoding="utf-8"))
        assert parsed["bundle_digest"] == manifest.bundle_digest
        assert final.name == bundle_store_key(manifest.bundle_digest)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_exec_bit_staged_from_manifest_entry(self):
        # Requirement: an executable=True entry is staged 0o755 and a
        # non-executable one 0o644, per the manifest entry, not the payload.
        manifest, files, links = _payload_manifest()
        final = publish_bundle(manifest, files, links)
        exec_mode = os.stat(final / "tree/main/scripts/run.sh").st_mode
        plain_mode = os.stat(final / "tree/main/plain.txt").st_mode
        assert stat.S_IMODE(exec_mode) == 0o755
        assert stat.S_IMODE(plain_mode) == 0o644

    @pytest.mark.skipif(not _POSIX, reason="symlink support is POSIX-first")
    def test_symlink_entry_materialized_in_store(self):
        # Requirement: a link entry is materialized with os.symlink at its
        # full logical path — the store tree is a faithful POSIX staging.
        manifest, files, links = _payload_manifest()
        final = publish_bundle(manifest, files, links)
        link = final / "tree/main/link.txt"
        assert link.is_symlink()
        assert os.readlink(link) == "plain.txt"

    def test_manifest_copy_inside_archive_matches_bundle_json(self):
        # Requirement: the archive carries .bundle/manifest.json byte-identical
        # to the bundle.json sentinel — publish stages the copy before archiving.
        manifest, files, links = _payload_manifest()
        final = publish_bundle(manifest, files, links)
        with tarfile.open(final / "bundle.tar.gz", "r:gz") as tar:
            member = tar.getmember(".bundle/manifest.json")
            extracted = tar.extractfile(member)
            assert extracted is not None
            archived = extracted.read()
        assert archived == (final / "bundle.json").read_bytes()
        assert archived == serialize_manifest(manifest).encode("utf-8")

    def test_manifest_files_use_lf_bytes(self, monkeypatch: pytest.MonkeyPatch):
        # Requirement: manifest copies are written as explicit UTF-8 bytes so
        # Windows newline translation cannot introduce CRLF into the store or archive.
        manifest, files, links = _payload_manifest()
        serialized = serialize_manifest(manifest)

        def serialize_with_crlf(_manifest: BundleManifest) -> str:
            return serialized.replace("\n", "\r\n")

        monkeypatch.setattr("conductor.bundle.store.serialize_manifest", serialize_with_crlf)

        final = publish_bundle(manifest, files, links)

        sentinel = (final / "bundle.json").read_bytes()
        with tarfile.open(final / "bundle.tar.gz", "r:gz") as tar:
            extracted = tar.extractfile(".bundle/manifest.json")
            assert extracted is not None
            archived = extracted.read()
        assert sentinel == archived
        assert b"\r" not in sentinel
        assert sentinel.count(b"\n") == serialized.count("\n")

    def test_publish_rejects_malformed_digest(self):
        # Requirement: the store key is the full sha256:<64 hex> digest; a
        # malformed digest is a caller bug and raises before any I/O.
        manifest, files, links = _payload_manifest()
        broken = manifest.model_copy(update={"bundle_digest": "sha256:abc"})
        with pytest.raises(ValueError, match="sha256"):
            publish_bundle(broken, files, links)

    def test_publish_rejects_payload_not_in_manifest(self):
        # Requirement: payloads and links must be declared manifest entries of
        # the matching kind — a silent mode/content mismatch is a caller bug.
        manifest, files, links = _payload_manifest()
        with pytest.raises(ValueError, match="not a declared"):
            publish_bundle(manifest, {**files, "tree/main/extra.txt": b"x"}, links)

    def test_publish_rejects_entry_escaping_staging(self):
        # Requirement: even a hand-built manifest that bypasses the model
        # validator (model_construct) must not make the store write outside
        # its temp tree — _stage_tree resolves each target and raises.
        entry = BundleEntry.model_construct(
            logical_path="../evil.txt",
            kind="file",
            digest=f"sha256:{hashlib.sha256(b'x').hexdigest()}",
            size=1,
            executable=False,
            link_target=None,
            origin_kind="asset",
            origin_detail="asset:../evil.txt",
        )
        manifest = BundleManifest.model_construct(
            version=1,
            bundle_digest=compute_bundle_digest([entry], {}, {}),
            entries=(entry,),
            skills_topology={},
            plugins_topology={},
        )

        with pytest.raises(ValueError, match="outside"):
            publish_bundle(manifest, {"../evil.txt": b"x"}, {})

        base = bundle_store_base()
        assert not (base.parent / "evil.txt").exists()
        quarantine_leftovers = [item.name for item in base.iterdir() if "quarantine" in item.name]
        assert quarantine_leftovers == []


class TestReuse:
    def test_second_publish_reuses_valid_dir(self):
        # Requirement: republishing the same digest returns the existing path
        # and does not rewrite bundle.json — its mtime is unchanged (the
        # is_cached precedent: validate by parsing, never re-hash the tree).
        manifest, files, links = _payload_manifest()
        first = publish_bundle(manifest, files, links)
        sentinel = first / "bundle.json"
        mtime_ns = sentinel.stat().st_mtime_ns

        second = publish_bundle(manifest, files, links)

        assert second == first
        assert sentinel.stat().st_mtime_ns == mtime_ns

    def test_invalid_preexisting_dir_warns_and_rebuilds(self, caplog):
        # Requirement: a pre-existing dir whose bundle.json carries a
        # DIFFERENT digest is invalid — the store warns (module logger) and
        # self-heals by removing and rebuilding the tree.
        manifest, files, links = _payload_manifest()
        final = bundle_store_path(manifest.bundle_digest)
        final.mkdir(parents=True)
        (final / "bundle.json").write_text(
            json.dumps({"bundle_digest": "sha256:" + "f" * 64}), encoding="utf-8"
        )

        with caplog.at_level(logging.WARNING, logger="conductor.bundle.store"):
            result = publish_bundle(manifest, files, links)

        assert result == final
        assert any("Invalid bundle store directory" in rec.message for rec in caplog.records)
        parsed = json.loads((final / "bundle.json").read_text(encoding="utf-8"))
        assert parsed["bundle_digest"] == manifest.bundle_digest
        assert (final / "tree/main/workflow.yaml").is_file()

    def test_invalid_dir_is_quarantined_and_removed(self, caplog, monkeypatch):
        # Requirement: an invalid pre-existing directory is renamed aside to a
        # quarantine name (never rmtree'd in place, so a concurrent publisher
        # mid-publish is not deleted) and the quarantine is then removed —
        # after publish returns the final dir is valid and no quarantine
        # remains. The spies prove the rename-aside: the final directory must
        # reach rmtree only via its quarantine name.
        import conductor.bundle.store as store_module

        manifest, files, links = _payload_manifest()
        final = bundle_store_path(manifest.bundle_digest)
        final.mkdir(parents=True)
        (final / "stale-partial.txt").write_text("partial", encoding="utf-8")
        (final / "bundle.json").write_text("not json", encoding="utf-8")

        rename_calls: list[tuple[str, str]] = []
        rmtree_targets: list[str] = []
        real_rename = os.rename
        real_rmtree = shutil.rmtree

        def spy_rename(src, dst):
            rename_calls.append((str(src), str(dst)))
            return real_rename(src, dst)

        def spy_rmtree(path, *args, **kwargs):
            rmtree_targets.append(str(path))
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(store_module.os, "rename", spy_rename)
        monkeypatch.setattr(store_module.shutil, "rmtree", spy_rmtree)

        with caplog.at_level(logging.WARNING, logger="conductor.bundle.store"):
            result = publish_bundle(manifest, files, links)

        assert result == final
        quarantine_renames = [
            (src, dst) for src, dst in rename_calls if "quarantine" in Path(dst).name
        ]
        rename_dst_dirs = [
            Path(dst).name.lstrip(".").split(".quarantine")[0] for _, dst in quarantine_renames
        ]
        assert rename_dst_dirs == [final.name]
        assert [src for src, _ in quarantine_renames] == [str(final)]
        assert str(final) not in rmtree_targets
        parsed = json.loads((final / "bundle.json").read_text(encoding="utf-8"))
        assert parsed["bundle_digest"] == manifest.bundle_digest
        assert not (final / "stale-partial.txt").exists()
        leftovers = [
            entry.name for entry in bundle_store_base().iterdir() if "quarantine" in entry.name
        ]
        assert leftovers == []

    def test_warning_sink_override_receives_message(self):
        # Requirement: the invalid-dir warning is delivered to an explicit
        # on_warning sink when one is passed, instead of only the logger.
        manifest, files, links = _payload_manifest()
        final = bundle_store_path(manifest.bundle_digest)
        final.mkdir(parents=True)
        (final / "bundle.json").write_text("not json", encoding="utf-8")
        messages: list[str] = []

        publish_bundle(manifest, files, links, on_warning=messages.append)

        assert any("Invalid bundle store directory" in message for message in messages)
        parsed = json.loads((final / "bundle.json").read_text(encoding="utf-8"))
        assert parsed["bundle_digest"] == manifest.bundle_digest


class TestCrashAndRace:
    def test_digest_lock_serializes_processes(self, tmp_path: Path):
        # Requirement: a digest lock held by one process blocks a second
        # process until validation, repair, and publication may safely proceed.
        from conductor.bundle.store import _digest_lock

        lock_path = tmp_path / ".sha256-test.lock"
        ready_path = tmp_path / "ready"
        acquired_path = tmp_path / "acquired"
        script = (
            "from pathlib import Path\n"
            "from conductor.bundle.store import _digest_lock\n"
            f"lock = Path({str(lock_path)!r})\n"
            f"ready = Path({str(ready_path)!r})\n"
            f"acquired = Path({str(acquired_path)!r})\n"
            "ready.write_bytes(b'ready')\n"
            "with _digest_lock(lock):\n"
            "    acquired.write_bytes(b'acquired')\n"
        )

        with _digest_lock(lock_path):
            process = subprocess.Popen([sys.executable, "-c", script])
            deadline = time.monotonic() + 5
            while not ready_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready_path.is_file()
            time.sleep(0.1)
            assert not acquired_path.exists()

        assert process.wait(timeout=5) == 0
        assert acquired_path.read_bytes() == b"acquired"

    def test_crash_mid_write_leaves_no_residue(self, monkeypatch):
        # Requirement: an exception between file writes removes the staging
        # dir and leaves no final dir or temp files behind, and the next
        # publish succeeds cleanly (cache self-healing).
        manifest, files, links = _payload_manifest()

        def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated crash mid-write")

        # A nested context, so reverting the crash patch cannot also revert
        # the conftest autouse CONDUCTOR_HOME isolation on this monkeypatch.
        with monkeypatch.context() as crash_patch:
            crash_patch.setattr("conductor.bundle.store.write_bundle_archive", _boom)
            with pytest.raises(RuntimeError, match="simulated crash"):
                publish_bundle(manifest, files, links)

        base = bundle_store_base()
        assert not bundle_store_path(manifest.bundle_digest).exists()
        leftovers = [entry.name for entry in base.iterdir() if entry.name.startswith("tmp")]
        assert leftovers == []
        archive_leftovers = [
            entry.name for entry in base.iterdir() if entry.name.startswith(".tmp")
        ]
        assert archive_leftovers == []

        final = publish_bundle(manifest, files, links)
        assert (final / "bundle.json").is_file()

    def test_concurrent_publishers_both_get_valid_store_path(self):
        # Requirement: two threads publishing the same digest concurrently
        # race on the atomic rename — the loser switches to validate-and-return
        # the winner's dir, so both callers receive a valid store path.
        manifest, files, links = _payload_manifest()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(publish_bundle, manifest, files, links) for _ in range(2)]
            results = [future.result() for future in futures]

        final = bundle_store_path(manifest.bundle_digest)
        assert results[0] == results[1] == final
        parsed = json.loads((final / "bundle.json").read_text(encoding="utf-8"))
        assert parsed["bundle_digest"] == manifest.bundle_digest

    def test_repair_lock_preserves_valid_bundle_when_second_writer_would_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Requirement: repair and publication are serialized per digest and
        # validity is rechecked under the lock, so a later failing writer
        # cannot quarantine or remove the valid bundle published by the winner.
        import conductor.bundle.store as store_module

        manifest, files, links = _payload_manifest()
        final = bundle_store_path(manifest.bundle_digest)
        final.mkdir(parents=True)
        (final / "bundle.json").write_text("not json", encoding="utf-8")
        first_in_archive = threading.Event()
        release_first = threading.Event()
        real_write_archive = store_module.write_bundle_archive
        archive_calls = 0
        archive_calls_lock = threading.Lock()

        def controlled_write_archive(
            staged_tree: Path,
            out: Path,
            *,
            executable_paths: set[str],
        ) -> str:
            nonlocal archive_calls
            with archive_calls_lock:
                archive_calls += 1
                call_number = archive_calls
            if call_number == 1:
                first_in_archive.set()
                assert release_first.wait(timeout=5)
                return real_write_archive(
                    staged_tree,
                    out,
                    executable_paths=executable_paths,
                )
            raise RuntimeError("second writer must reuse the repaired bundle")

        monkeypatch.setattr(store_module, "write_bundle_archive", controlled_write_archive)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(publish_bundle, manifest, files, links)
            assert first_in_archive.wait(timeout=5)
            second = pool.submit(publish_bundle, manifest, files, links)
            release_first.set()
            results = [first.result(timeout=5), second.result(timeout=5)]

        assert results == [final, final]
        assert archive_calls == 1
        assert (
            json.loads((final / "bundle.json").read_text(encoding="utf-8"))["bundle_digest"]
            == manifest.bundle_digest
        )
