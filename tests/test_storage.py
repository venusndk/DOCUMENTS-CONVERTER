"""
Tests for the storage abstraction (documents_converter/api/storage.py) --
Phase 3 completion, master directive numbering.
"""

from __future__ import annotations

from documents_converter.api.storage import LocalDiskStorage


def test_allocate_creates_a_real_directory():
    backend = LocalDiskStorage()
    path = backend.allocate("test-prefix-")
    try:
        assert path.is_dir()
        assert path.name.startswith("test-prefix-")
    finally:
        backend.release(path)


def test_allocate_returns_distinct_directories_each_call():
    backend = LocalDiskStorage()
    a = backend.allocate("docconv-")
    b = backend.allocate("docconv-")
    try:
        assert a != b
        assert a.is_dir() and b.is_dir()
    finally:
        backend.release(a)
        backend.release(b)


def test_release_removes_the_directory_and_its_contents():
    backend = LocalDiskStorage()
    path = backend.allocate("docconv-")
    (path / "output.xlsx").write_bytes(b"fake spreadsheet bytes")

    backend.release(path)

    assert not path.exists()


def test_release_on_an_already_removed_path_does_not_raise():
    backend = LocalDiskStorage()
    path = backend.allocate("docconv-")
    backend.release(path)

    backend.release(path)  # must not raise


def test_release_on_a_never_allocated_path_does_not_raise(tmp_path):
    backend = LocalDiskStorage()
    backend.release(tmp_path / "never-existed")  # must not raise
