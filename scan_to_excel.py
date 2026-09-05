#!/usr/bin/env python3
"""
scan_to_excel.py
-----------------
Thin backward-compatible CLI entry point. The actual implementation lives in
documents_converter/ocr_excel.py (Phase 1 restructuring, see
docs/PHASE_0_AUDIT.md) -- this file exists purely so `python scan_to_excel.py
...` keeps working exactly as it always has, with no behavior change.

Run `python scan_to_excel.py -h` for usage.
"""

import sys
from pathlib import Path

# So this script works when run directly (`python scan_to_excel.py ...`)
# from any working directory, not just when the package is pip-installed.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from documents_converter.ocr_excel import main

if __name__ == "__main__":
    main()
