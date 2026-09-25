"""Tests for :mod:`conductor.bundle.archive` — the deterministic bundle writer.

The load-bearing invariant: two builds over the same staged tree produce
byte-identical archives (pinned gzip mtime, pinned TarInfo ownership/mtime,
POSIX-path sort order), and the manifest copy inside the archive is whatever
the caller staged at ``.bundle/manifest.json`` — ``publish_bundle`` stages it
byte-identical to ``bundle.json`` so the archive and the store sentinel agree.
"""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

import pytest

from conductor.bundle.archive import write_bundle_archive

_POSIX = hasattr(os, "symlink")


def _stage_tree(tmp_path: Path, *, with_symlink: bool = True) -> Path:
    """Build a staged bundle tree: nested files, exec bit, manifest copy, link."""
    tree = tmp_path / "staged"
    (tree / "tree/main/scripts").mkdir(parents=True)
    (tree / "tree/main/workflow.yaml").write_bytes(b"workflow: {entry_point: a}\n")
    script = tree / "tree/main/scripts/run.sh"
    script.write_bytes(b"#!/bin/sh\necho hi\n")
    os.chmod(script, 0o755)
    plain = tree / "tree/main/plain.txt"
    plain.write_bytes(b"plain\n")
    os.chmod(plain, 0o644)
    (tree / ".bundle").mkdir()
    (tree / ".bundle/manifest.json").write_bytes(b'{"version": 1}\n')
    if with_symlink and _POSIX:
        os.symlink("plain.txt", tree / "tree/main/link.txt")
    return tree


def _read_members(out: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(out, "r:gz") as tar:
        return {member.name: member for member in tar.getmembers()}


class TestDeterminism:
    def test_two_builds_produce_byte_identical_archives(self, tmp_path: Path):
        # Requirement: two builds of the same staged tree must yield
        # byte-identical archives — the run bundle archive is reproducible.
        tree = _stage_tree(tmp_path)
        first_out = tmp_path / "first.tar.gz"
        second_out = tmp_path / "second.tar.gz"
        executable_paths = {"tree/main/scripts/run.sh"}
        first = write_bundle_archive(tree, first_out, executable_paths=executable_paths)
        second = write_bundle_archive(tree, second_out, executable_paths=executable_paths)
        assert first == second
        assert first_out.read_bytes() == second_out.read_bytes()
        assert first.startswith("sha256:") and len(first) == len("sha256:") + 64

    def test_gzip_header_has_no_name_or_timestamp(self, tmp_path: Path):
        # Requirement: the gzip layer pins mtime=0 and writes no original
        # filename, so nothing host- or clock-dependent enters the bytes.
        tree = _stage_tree(tmp_path)
        out = tmp_path / "bundle.tar.gz"
        write_bundle_archive(tree, out, executable_paths={"tree/main/scripts/run.sh"})
        header = out.read_bytes()[:10]
        assert header[:3] == b"\x1f\x8b\x08"  # magic + deflate
        assert header[4:8] == b"\x00\x00\x00\x00"  # MTIME pinned to 0
        flags = header[3]
        assert flags & 0b1000 == 0  # FNAME not set
        assert flags & 0b1000000 == 0  # FComment not set


class TestMemberLayout:
    def test_members_sorted_by_posix_arcname_and_directories_omitted(self, tmp_path: Path):
        # Requirement: entries appear byte-wise sorted by POSIX logical path
        # and directory entries are omitted — only files and symlinks are
        # archived.
        tree = _stage_tree(tmp_path)
        out = tmp_path / "bundle.tar.gz"
        write_bundle_archive(tree, out, executable_paths={"tree/main/scripts/run.sh"})
        with tarfile.open(out, "r:gz") as tar:
            names = [member.name for member in tar.getmembers()]
        assert names == sorted(names)
        assert all(not name.endswith("/") for name in names)
        assert "tree/main/scripts" not in names
        assert ".bundle/manifest.json" in names

    def test_manifest_copy_inside_archive_matches_staged_bytes(self, tmp_path: Path):
        # Requirement: the caller stages .bundle/manifest.json before
        # archiving, so the archived copy is byte-identical to what the store
        # later writes as bundle.json (publish_bundle relies on this).
        tree = _stage_tree(tmp_path)
        out = tmp_path / "bundle.tar.gz"
        write_bundle_archive(tree, out, executable_paths={"tree/main/scripts/run.sh"})
        with tarfile.open(out, "r:gz") as tar:
            member = tar.getmember(".bundle/manifest.json")
            extracted = tar.extractfile(member)
            assert extracted is not None
            archived = extracted.read()
        assert archived == (tree / ".bundle/manifest.json").read_bytes()
        assert archived == b'{"version": 1}\n'

    def test_tar_metadata_is_pinned(self, tmp_path: Path):
        # Requirement: every member carries mtime=0, uid=gid=0 and empty
        # uname/gname so no host identity leaks into the archive.
        tree = _stage_tree(tmp_path)
        out = tmp_path / "bundle.tar.gz"
        write_bundle_archive(tree, out, executable_paths={"tree/main/scripts/run.sh"})
        for member in _read_members(out).values():
            assert member.mtime == 0
            assert member.uid == 0 and member.gid == 0
            assert member.uname == "" and member.gname == ""


class TestModesAndTypes:
    def test_exec_bit_maps_to_tar_modes(self, tmp_path: Path):
        # Requirement: executable paths declared by the manifest archive as
        # 0o755 and every other regular file as 0o644.
        tree = _stage_tree(tmp_path)
        out = tmp_path / "bundle.tar.gz"
        write_bundle_archive(tree, out, executable_paths={"tree/main/scripts/run.sh"})
        members = _read_members(out)
        assert members["tree/main/scripts/run.sh"].mode == 0o755
        assert members["tree/main/plain.txt"].mode == 0o644

    @pytest.mark.skipif(not _POSIX, reason="symlink support is POSIX-first")
    def test_symlink_entry_archives_as_symtype(self, tmp_path: Path):
        # Requirement: a staged symlink archives as a SYMTYPE member with the
        # link target as linkname and mode 0o777 (its content is the target).
        tree = _stage_tree(tmp_path)
        out = tmp_path / "bundle.tar.gz"
        write_bundle_archive(tree, out, executable_paths={"tree/main/scripts/run.sh"})
        member = _read_members(out)["tree/main/link.txt"]
        assert member.issym()
        assert member.type == tarfile.SYMTYPE
        assert member.linkname == "plain.txt"
        assert member.mode == 0o777
        assert member.size == 0

    def test_archive_mode_ignores_host_filesystem_mode(self, tmp_path: Path):
        # Requirement: archive modes come only from the manifest-derived path
        # set, so Windows chmod behavior cannot change the resulting tar mode.
        tree = tmp_path / "staged"
        tree.mkdir()
        exe = tree / "a.bin"
        exe.write_bytes(b"same")
        os.chmod(exe, 0o755)
        out = tmp_path / "bundle.tar.gz"
        write_bundle_archive(tree, out, executable_paths={"a.bin"})
        assert _read_members(out)["a.bin"].mode == 0o755
        os.chmod(exe, 0o644)
        out2 = tmp_path / "bundle2.tar.gz"
        write_bundle_archive(tree, out2, executable_paths={"a.bin"})
        assert _read_members(out2)["a.bin"].mode == 0o755

        out3 = tmp_path / "bundle3.tar.gz"
        write_bundle_archive(tree, out3, executable_paths=set())
        assert _read_members(out3)["a.bin"].mode == 0o644
