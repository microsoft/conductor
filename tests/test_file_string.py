"""Unit tests for the FileString class carrying source path metadata."""

from __future__ import annotations

import copy
import pickle
from pathlib import Path

from conductor.file_string import FileString


def test_construction_and_source_path() -> None:
    # Requirement: FileString construction accepts str and Path, normalizes to Path,
    # and exposes the source_path attribute.
    fs_path = FileString("hello", Path("/tmp/x.md"))
    assert fs_path.source_path == Path("/tmp/x.md")

    fs_str = FileString("world", "/tmp/y.md")
    assert fs_str.source_path == Path("/tmp/y.md")


def test_isinstance_str() -> None:
    # Requirement: FileString must be a subclass of str and isinstance(..., str) must be True.
    fs = FileString("test", Path("a.txt"))
    assert isinstance(fs, str)
    assert isinstance(fs, FileString)


def test_equality_and_hashing() -> None:
    # Requirement: Equality and hashing must work like plain str, and the subclass adds
    # no custom equality semantics (i.e. equality ignores source_path).
    fs1 = FileString("abc", Path("x"))
    fs2 = FileString("abc", Path("y"))

    assert fs1 == "abc"
    assert fs1 == fs2
    assert hash(fs1) == hash("abc")

    # Verify different string values are not equal
    fs3 = FileString("def", Path("x"))
    assert fs1 != fs3


def test_deepcopy_preserves_attributes() -> None:
    # Requirement: copy.deepcopy(FileString) must return a FileString with the same value
    # and the same source_path, and must not crash.
    fs = FileString("value", Path("origin.md"))
    fs_deep = copy.deepcopy(fs)

    assert isinstance(fs_deep, FileString)
    assert fs_deep == "value"
    assert fs_deep.source_path == Path("origin.md")


def test_copy_preserves_attributes() -> None:
    # Requirement: copy.copy (shallow copy) must also preserve the source_path.
    fs = FileString("value", Path("origin.md"))
    fs_shallow = copy.copy(fs)

    assert isinstance(fs_shallow, FileString)
    assert fs_shallow == "value"
    assert fs_shallow.source_path == Path("origin.md")


def test_pickle_roundtrip() -> None:
    # Requirement: Pickling and unpickling a FileString must preserve its string value
    # and its source_path metadata.
    fs = FileString("to_pickle", Path("safe.txt"))
    dumped = pickle.dumps(fs)
    loaded = pickle.loads(dumped)

    assert isinstance(loaded, FileString)
    assert loaded == "to_pickle"
    assert loaded.source_path == Path("safe.txt")
