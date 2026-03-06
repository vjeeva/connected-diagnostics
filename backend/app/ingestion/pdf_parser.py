"""Extract text from PDF page-by-page using pymupdf (fitz).

Falls back to Claude vision for image-only pages when ocr=True.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import fitz



@dataclass
class PageText:
    page_number: int
    text: str


def extract_pages(
    pdf_path: str,
    start_page: int = 1,
    end_page: int | None = None,
    ocr: bool = False,
) -> list[PageText]:
    """Extract text from each page of a PDF. Pages are 1-indexed.

    If ocr=True, image-only pages are rendered and sent to Claude vision.
    """
    pages: list[PageText] = []
    empty_pages: list[int] = []
    doc = fitz.open(pdf_path)
    total = doc.page_count
    end = min(end_page or total, total)

    for i in range(start_page - 1, end):
        page = doc.load_page(i)
        text = page.get_text("text") or ""
        if text.strip():
            pages.append(PageText(page_number=i + 1, text=text))
        else:
            empty_pages.append(i)

    if ocr and empty_pages:
        from backend.app.services.llm.client import vision

        for i in empty_pages:
            page = doc.load_page(i)
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes("png")
            img_b64 = base64.b64encode(img_bytes).decode()

            text = vision(
                system=_OCR_SYSTEM,
                image_b64=img_b64,
                prompt=f"Extract all text and diagram details from this service manual page (page {i + 1}).",
            )
            if text.strip():
                pages.append(PageText(page_number=i + 1, text=text))

        pages.sort(key=lambda p: p.page_number)

    doc.close()
    return pages


_OCR_SYSTEM = """You are extracting text content from a scanned service manual page image.

Extract ALL text visible on the page, preserving the structure:
- Section headers and titles
- DTC codes
- Step numbers and instructions
- Measurement values, pin numbers, wire colors
- Table data (connector pinouts, resistance values, voltage specs)
- Diagram labels and callouts

For wiring diagrams or connector diagrams, describe:
- Connector names and pin assignments
- Wire colors and gauge
- Circuit routing

Return the extracted text as plain text, maintaining the logical reading order."""


