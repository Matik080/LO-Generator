import logging
import os
from typing import List, Dict, Any

import PyPDF2

from lo_pipeline.extraction import ocr as ocr_module

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    """A document could not be read, or no text was found in it (job error code extraction_failed)."""


LETTER_RUN_MIN_LINES = 6  # vertical text: at least this many lines in a row ...
LETTER_RUN_MAX_CHARS = 5  # ... each this short (title pieces like "cisio", "rizik")


def join_letter_runs(text: str) -> str:
    """Vertical (rotated) text, e.g. a slide title along the side, comes out one letter per line
    ("R\\no\\nz\\nh…"). Runs of LETTER_RUN_MIN_LINES or more very short lines are joined into one line.
    Word spaces are mostly lost there ("Odtestovaniakrozhodnutiu…"), but the text stays readable."""
    lines = text.split("\n")
    out, run = [], []

    def flush():
        if len(run) >= LETTER_RUN_MIN_LINES:
            out.append("".join(line.strip() for line in run))
        else:
            out.extend(run)
        run.clear()

    for line in lines:
        if 0 < len(line.strip()) <= LETTER_RUN_MAX_CHARS:
            run.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


def pdf_page_texts(pdf_path: str) -> List[str]:
    """Text of each page (index 0 = page 1). PDFium (pypdfium2) when available: it keeps words of
    PowerPoint exports together, where PyPDF2 gives "Rozh o d o v a n i e". PyPDF2 otherwise."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        pdfium = None
    if pdfium is not None:
        try:
            pdf = pdfium.PdfDocument(pdf_path)
            try:
                texts = []
                for index in range(len(pdf)):
                    page = pdf[index]
                    textpage = page.get_textpage()
                    texts.append(join_letter_runs(textpage.get_text_range().replace("\r\n", "\n").replace("\r", "\n")))
                    textpage.close()
                    page.close()
                return texts
            finally:
                pdf.close()
        except Exception as e:  # a file PDFium cannot read: try PyPDF2
            logger.warning(f"[Extract] PDFium could not read {os.path.basename(pdf_path)} ({e}); using PyPDF2.")
    with open(pdf_path, "rb") as f:
        return [page.extract_text() or "" for page in PyPDF2.PdfReader(f).pages]


OCR_MIN_CHARS = 20  # with OCR, a page with less text than this (a scan) is read by OCR


def extract_text_from_pdf(pdf_path: str, document_title: str = None, ocr: bool = False) -> str:
    """Text per page. With `ocr`, pages without a text layer are read by OCR (extraction/ocr.py);
    their page numbers stay the same. A page that OCR cannot read is skipped (logged)."""
    try:
        text = ""
        title = document_title or os.path.basename(pdf_path)
        for page_num, page_text in enumerate(pdf_page_texts(pdf_path), 1):
            if ocr and len("".join(page_text.split())) < OCR_MIN_CHARS:
                page_text = _ocr_or_empty(lambda: ocr_module.ocr_pdf_page(pdf_path, page_num - 1),
                                          f"page {page_num} of {title}") or page_text
            if page_text.strip():
                text += f"\n\n--- Page {page_num} | Source: {title} ---\n\n"
                text += page_text
        return text
    except Exception as e:
        raise ExtractionError(f"Failed to extract text from {pdf_path}: {e}") from e

def _picture_blob(shape):
    """Image bytes of a picture shape (also a picture placeholder); None for other shapes."""
    try:
        return shape.image.blob
    except Exception:  # no picture in this shape
        return None


def _ocr_or_empty(read, what: str) -> str:
    try:
        return read()
    except ocr_module.OcrError as e:
        logger.warning(f"[Extract] OCR failed for {what}: {e}")
        return ""


def extract_text_from_html(html_path: str, document_title: str = None) -> str:
    try:
        from bs4 import BeautifulSoup

        with open(html_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")

        title = document_title or os.path.basename(html_path)

        # Remove non-content elements
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()

        text = ""
        current_section = []
        section_num = 1

        for element in soup.find_all(["h1", "h2", "h3", "h4", "p", "pre", "code", "li", "table"]):
            if element.name in ("h1", "h2", "h3"):
                if current_section:
                    section_text = " ".join(current_section).strip()
                    if section_text:
                        text += f"\n\n--- Page {section_num} | Source: {title} ---\n\n"
                        text += section_text
                        section_num += 1
                current_section = [element.get_text(strip=True)]
            else:
                content = element.get_text(strip=True)
                if content:
                    current_section.append(content)

        # Last section
        if current_section:
            section_text = " ".join(current_section).strip()
            if section_text:
                text += f"\n\n--- Page {section_num} | Source: {title} ---\n\n"
                text += section_text

        return text
    except ImportError:
        raise ExtractionError("beautifulsoup4 not installed. Run: pip install beautifulsoup4")
    except Exception as e:
        raise ExtractionError(f"Failed to extract text from {html_path}: {e}") from e


def extract_text_from_pptx(pptx_path: str, document_title: str = None, ocr: bool = False) -> str:
    """Text per slide (text frames, tables, speaker notes). With `ocr`, the text in pictures too
    (e.g. a screenshot or a scanned diagram), read by OCR (extraction/ocr.py)."""
    try:
        from pptx import Presentation

        prs = Presentation(pptx_path)
        title = document_title or os.path.basename(pptx_path)
        text = ""

        for slide_num, slide in enumerate(prs.slides, 1):
            slide_text = []

            for shape in slide.shapes:
                blob = _picture_blob(shape) if ocr else None
                if blob:
                    image_text = _ocr_or_empty(lambda: ocr_module.ocr_image(blob),
                                               f"a picture on slide {slide_num} of {title}")
                    if image_text.strip():
                        slide_text.append(image_text.strip())
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        para_text = para.text.strip()
                        if para_text:
                            slide_text.append(para_text)

                if shape.has_table:
                    for row in shape.table.rows:
                        row_text = " | ".join(
                            cell.text.strip()
                            for cell in row.cells
                            if cell.text.strip()
                        )
                        if row_text:
                            slide_text.append(row_text)

            # Speaker notes often explain what graphs show
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    slide_text.append(f"[Notes: {notes}]")

            if slide_text:
                text += f"\n\n--- Page {slide_num} | Source: {title} ---\n\n"
                text += "\n".join(slide_text)

        return text
    except ImportError:
        raise ExtractionError("python-pptx not installed. Run: pip install python-pptx")
    except Exception as e:
        raise ExtractionError(f"Failed to extract text from {pptx_path}: {e}") from e


def extract_text_from_source(path: str, document_title: str = None, ocr: bool = False) -> str:
    """Unified text extraction, dispatches based on file extension. `ocr`: also read scanned PDF
    pages and pictures in PPTX (HTML has no images to read)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return extract_text_from_pdf(path, document_title, ocr=ocr)
    elif ext in (".html", ".htm"):
        return extract_text_from_html(path, document_title)
    elif ext in (".pptx", ".ppt"):
        return extract_text_from_pptx(path, document_title, ocr=ocr)
    else:
        raise ValueError(f"Unsupported file type: {ext}. Supported: .pdf, .html, .htm, .pptx")

# Splitting extracted text into sections based on pages, for now
def split_into_sections(text: str) -> List[Dict[str, Any]]:
    """Split text into sections, preserving page number and source title."""
    raw_sections = text.split("--- Page")
    sections = []
    for s in raw_sections:
        s = s.strip()
        if not s:
            continue
        # Parse header line: "42 | Source: Practical C Programming ---"
        lines = s.split("\n", 1)
        header = lines[0]
        content = lines[1].strip() if len(lines) > 1 else ""

        page_num = None
        source = None
        try:
            # "42 | Source: Practical C Programming ---"
            parts = header.split("|")
            page_num = int(parts[0].strip())
            source = parts[1].replace("Source:", "").replace("---", "").strip()
        except Exception:
            pass

        sections.append({
            "page": page_num,
            "source": source,
            "content": content
        })
    return sections