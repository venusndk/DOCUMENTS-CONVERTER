"""
Registers every known Capability into the shared registry
(documents_converter/registry.py) on import.

Adding a new conversion means: write a new module here exposing a
CAPABILITY, then add one line below. Nothing outside this file needs to
change to make a new conversion reachable through the registry -- no
edits to app.py's routing logic, which only ever asks the registry
"what handles (ext, target)?" (see registry.CapabilityRegistry.find).
"""

from __future__ import annotations

from ..registry import registry
from . import (
    html_to_pdf,
    image_to_pdf,
    markdown_to_pdf,
    ocr_to_excel,
    office_to_pdf,
    pdf_to_docx,
    pdf_to_images,
    pdf_to_pptx,
    pdf_to_text,
    searchable_pdf,
)

registry.register(ocr_to_excel.CAPABILITY)
registry.register(image_to_pdf.CAPABILITY)
registry.register(searchable_pdf.CAPABILITY)
registry.register(pdf_to_images.CAPABILITY)
registry.register(pdf_to_text.CAPABILITY)
registry.register(office_to_pdf.CAPABILITY)
registry.register(html_to_pdf.CAPABILITY)
registry.register(markdown_to_pdf.CAPABILITY)
registry.register(pdf_to_docx.CAPABILITY)
registry.register(pdf_to_pptx.CAPABILITY)
