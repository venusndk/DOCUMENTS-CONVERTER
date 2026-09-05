"""Provider interfaces for documents_converter, per docs/PHASE_0_AUDIT.md
Phase 2: narrow seams around the two things ocr_excel.py currently calls
a specific engine for directly (per-cell OCR, and table detection), so a
different engine can be substituted later without touching the pipeline
logic that orchestrates them.
"""
