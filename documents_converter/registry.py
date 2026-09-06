"""
Format & Capability Registry.

Phase 9 (continuing the numbering from docs/PHASE_0_AUDIT.md's Phases
0-8, which were entirely about the OCR->Excel pipeline). This is the
first piece of the "Universal Document Intelligence Platform" directive
that isn't OCR-specific: a single source of truth for which
(source format -> target format) conversions this service can perform,
and what actually performs each one.

Why this exists (directive Sec 18, and repeated back to the user
before this phase started): without it, adding a second conversion the
"obvious" way means hardcoding another special case next to the OCR one
in app.py -- `if ext == ".pdf" and target == "xlsx": ... elif ext in
IMAGE_EXTS and target == "pdf": ...` -- and that branching only gets
worse with every conversion added after. Every caller (the HTTP API,
the CLI, tests, and eventually a frontend format picker) should be able
to ask the registry what's possible and get back the same answer,
instead of each one re-deriving it.

Deliberately small: no plugin auto-discovery, no config files, no
priority/ranking between competing providers for the same pair. Real
needs identified while building this project's actual OCR pipeline
(config-driven Tesseract path, provider swapping in
documents_converter/providers/) are already served by other modules;
this registry's only job is routing a (input extension, target format)
pair to the one function that handles it, and reporting what it knows
for discovery/documentation/validation purposes -- exactly the roles
the directive assigns it, no more.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


class ConvertFn(Protocol):
    def __call__(self, input_path: Path, output_path: Path, *, progress: Callable[[str], None]) -> None: ...


@dataclass(frozen=True)
class Capability:
    """One registered (source_format -> target_format) conversion.

    `source_format`/`target_format` are logical names (e.g.
    "scanned_document", "image", "xlsx", "pdf"), not file extensions --
    the same logical source format can legitimately accept several
    extensions (a scanned document might be a PDF or a photographed
    image of a paper page). `source_extensions` is what actually gates
    which uploaded files this capability applies to.
    """

    source_format: str
    target_format: str
    description: str
    source_extensions: frozenset[str]
    output_extension: str
    media_type: str
    convert: ConvertFn

    def accepts(self, ext: str) -> bool:
        return ext.lower() in self.source_extensions


class CapabilityRegistry:
    """Holds the set of registered capabilities and answers routing
    questions about them. Not thread-safety-sensitive: capabilities are
    registered once at import time and never mutated afterward, so
    lookups need no locking."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], Capability] = {}

    def register(self, capability: Capability) -> None:
        key = (capability.source_format, capability.target_format)
        if key in self._by_key:
            raise ValueError(f"Capability already registered for {key!r}")
        self._by_key[key] = capability

    def list_all(self) -> list[Capability]:
        return list(self._by_key.values())

    def find(self, target_format: str, ext: str) -> Capability | None:
        """The routing question app.py and the CLI actually need: given
        an uploaded file's extension and the target format the caller
        asked for, which capability (if any) handles it?

        Raises ValueError if more than one registered capability claims
        the same (target_format, ext) pair -- an ambiguous registration
        that should be caught at registration/testing time, not
        silently resolved by picking one.
        """
        matches = [
            c
            for c in self._by_key.values()
            if c.target_format == target_format and c.accepts(ext)
        ]
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous capability for target={target_format!r} ext={ext!r}: "
                f"{[c.source_format for c in matches]}"
            )
        return matches[0] if matches else None


# Module-level registry every part of the service shares -- the single
# source of truth the directive calls for. Populated by
# documents_converter/converters/__init__.py at import time so this
# module itself stays free of any specific conversion's implementation
# details.
registry = CapabilityRegistry()
