"""PDF text: vertical text (one letter per line) is joined; PDFium is used, PyPDF2 is the fallback."""
from lo_pipeline.extraction import documents
from lo_pipeline.extraction.documents import join_letter_runs


def test_vertical_title_is_joined_and_normal_lines_stay():
    text = "R\no\nz\nh\no\nd\nnie\no\nn\na\ns\na\nd\není\n• Dáta: výsledky testov.\n• Riziká."
    assert join_letter_runs(text) == "Rozhodnieonasadení\n• Dáta: výsledky testov.\n• Riziká."


def test_short_runs_and_empty_lines_are_kept():
    text = "Table\nÁno\nNie\nÁno\n\nx = 1\ny = 2"  # 3 short lines: a table, not vertical text
    assert join_letter_runs(text) == text


def test_pypdf2_is_used_when_pdfium_cannot_read_the_file(monkeypatch, tmp_path):
    import sys
    import types

    class BrokenPdfium(types.ModuleType):
        def PdfDocument(self, path):
            raise RuntimeError("cannot read")

    class Page:
        def extract_text(self):
            return "page text"

    class Reader:
        def __init__(self, f):
            self.pages = [Page(), Page()]

    monkeypatch.setitem(sys.modules, "pypdfium2", BrokenPdfium("pypdfium2"))
    monkeypatch.setattr(documents.PyPDF2, "PdfReader", Reader)
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert documents.pdf_page_texts(str(pdf)) == ["page text", "page text"]
    text = documents.extract_text_from_pdf(str(pdf), "x.pdf")
    assert "--- Page 2 | Source: x.pdf ---" in text
