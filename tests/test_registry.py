"""
Tests for the capability registry (documents_converter/registry.py) --
Phase 9, docs/PHASE_0_AUDIT.md numbering continued. Pure unit tests
against CapabilityRegistry itself, independent of any real conversion
implementation or the API layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from documents_converter.registry import Capability, CapabilityRegistry


def _dummy_capability(
    source_format: str = "widget",
    target_format: str = "gadget",
    extensions: frozenset[str] = frozenset({".widget"}),
) -> Capability:
    def _convert(input_path: Path, output_path: Path, *, progress) -> None:
        progress("converting")

    return Capability(
        source_format=source_format,
        target_format=target_format,
        description="test capability",
        source_extensions=extensions,
        output_extension=".gadget",
        media_type="application/x-gadget",
        convert=_convert,
    )


def test_register_and_list_all():
    registry = CapabilityRegistry()
    cap = _dummy_capability()
    registry.register(cap)
    assert registry.list_all() == [cap]


def test_register_rejects_duplicate_source_target_pair():
    registry = CapabilityRegistry()
    registry.register(_dummy_capability())
    with pytest.raises(ValueError):
        registry.register(_dummy_capability())


def test_find_matches_by_target_and_extension():
    registry = CapabilityRegistry()
    cap = _dummy_capability()
    registry.register(cap)
    assert registry.find("gadget", ".widget") is cap


def test_find_is_case_insensitive_on_extension():
    registry = CapabilityRegistry()
    cap = _dummy_capability()
    registry.register(cap)
    assert registry.find("gadget", ".WIDGET") is cap


def test_find_returns_none_for_unknown_target():
    registry = CapabilityRegistry()
    registry.register(_dummy_capability())
    assert registry.find("nonexistent-format", ".widget") is None


def test_find_returns_none_for_unknown_extension():
    registry = CapabilityRegistry()
    registry.register(_dummy_capability())
    assert registry.find("gadget", ".notwidget") is None


def test_find_raises_on_ambiguous_registration():
    """Two capabilities that both claim the same target format and the
    same extension is a registration bug, not something find() should
    silently resolve by picking one."""
    registry = CapabilityRegistry()
    registry.register(_dummy_capability(source_format="widget-a"))
    registry.register(_dummy_capability(source_format="widget-b"))
    with pytest.raises(ValueError):
        registry.find("gadget", ".widget")


def test_the_real_registry_has_no_ambiguous_pairs():
    """Guards against a real regression in the actual registered
    capabilities (documents_converter/converters/__init__.py), not just
    the abstract data structure: importing it and asking find() for
    every registered (target, extension) combination must never raise."""
    from documents_converter import converters  # noqa: F401 -- registers capabilities
    from documents_converter.registry import registry as real_registry

    for capability in real_registry.list_all():
        for ext in capability.source_extensions:
            found = real_registry.find(capability.target_format, ext)
            assert found is not None


def test_the_real_registry_has_the_two_phase_9_capabilities():
    from documents_converter import converters  # noqa: F401
    from documents_converter.registry import registry as real_registry

    pairs = {(c.source_format, c.target_format) for c in real_registry.list_all()}
    assert ("scanned_document", "xlsx") in pairs
    assert ("image", "pdf") in pairs
