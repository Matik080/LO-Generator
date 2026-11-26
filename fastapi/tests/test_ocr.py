"""Optional OCR: scanned PDF pages and PPTX pictures (fake OCR), availability, and real Tesseract if installed."""
import io

import pytest

from lo_pipeline.extraction import documents, ocr
from lo_pipeline.extraction.documents import ExtractionError, extract_text_from_source, split_into_sections

PAGE_TEXT = "This page has a normal text layer with enough words."


@pytest.fixture
def scanned_pdf(monkeypatch, tmp_path):
    """A PDF whose pages 1 and 3 have no text layer (scans); OCR reads 'Scanned page N'."""
    monkeypatch.setattr(documents, "pdf_page_texts", lambda path: ["", PAGE_TEXT, "  \n "])
    read = []

    def fake_ocr(path, index):
        read.append(index + 1)
        return f"Scanned page {index + 1} read by OCR."
    monkeypatch.setattr(ocr, "ocr_pdf_page", fake_ocr)
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    return str(pdf), read


def pages(text):
    return [(s["page"], s["content"]) for s in split_into_sections(text)]


def test_without_ocr_scanned_pages_are_skipped(scanned_pdf):
    path, read = scanned_pdf
    assert pages(extract_text_from_source(path)) == [(2, PAGE_TEXT)]
    assert read == []


def test_with_ocr_scanned_pages_keep_their_page_numbers(scanned_pdf):
    path, read = scanned_pdf
    assert pages(extract_text_from_source(path, ocr=True)) == [
        (1, "Scanned page 1 read by OCR."), (2, PAGE_TEXT), (3, "Scanned page 3 read by OCR.")]
    assert read == [1, 3]  # the page with a text layer is not read by OCR


def test_page_that_ocr_cannot_read_is_skipped(scanned_pdf, monkeypatch):
    path, _ = scanned_pdf

    def failing(path, index):
        if index == 0:
            raise ocr.OcrError("unreadable")
        return "Page three text."
    monkeypatch.setattr(ocr, "ocr_pdf_page", failing)
    assert pages(extract_text_from_source(path, ocr=True)) == [(2, PAGE_TEXT), (3, "Page three text.")]


def png_bytes():
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_pptx_pictures_are_read_only_with_ocr(monkeypatch, tmp_path):
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])  # title only
    slide.shapes.title.text = "Release decision"
    slide.shapes.add_picture(io.BytesIO(png_bytes()), Inches(1), Inches(2))
    path = tmp_path / "deck.pptx"
    prs.save(path)
    monkeypatch.setattr(ocr, "ocr_image", lambda blob: "GO / NO-GO criteria" if blob.startswith(b"\x89PNG") else "")

    assert pages(extract_text_from_source(str(path))) == [(1, "Release decision")]
    assert pages(extract_text_from_source(str(path), ocr=True)) == [(1, "Release decision\nGO / NO-GO criteria")]


def test_no_text_without_ocr_is_an_extraction_error_in_the_runner(monkeypatch, tmp_path):
    from lo_pipeline.runner import run_pipeline
    monkeypatch.setattr(documents, "pdf_page_texts", lambda path: ["", " "])
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with pytest.raises(ExtractionError):
        run_pipeline([str(pdf)])  # job error extraction_failed


def test_not_available_without_tesseract(monkeypatch):
    ocr.ocr_languages.cache_clear()
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    try:
        assert ocr.ocr_languages() == "" and not ocr.ocr_available()
        with pytest.raises(ocr.OcrError):
            ocr._tesseract(png_bytes())
    finally:
        ocr.ocr_languages.cache_clear()


@pytest.mark.skipif(not ocr.ocr_available(), reason="Tesseract not installed (image built with INSTALL_OCR=0)")
def test_real_tesseract_reads_an_image():
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (900, 160), "white")
    try:
        font = ImageFont.load_default(size=64)
    except TypeError:  # older Pillow
        font = ImageFont.load_default()
    ImageDraw.Draw(image).text((20, 40), "Release decision", fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    assert "release" in ocr.ocr_image(buffer.getvalue()).lower()
    assert set(ocr.ocr_languages().split("+")) == {"slk", "eng"}
