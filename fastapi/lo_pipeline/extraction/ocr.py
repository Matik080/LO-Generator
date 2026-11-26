"""
Optional OCR (job option extraction.ocr): text of PDF pages without a text layer (scans) and of
pictures in PPTX slides.

Uses the Tesseract program (installed in the image with the Docker build argument INSTALL_OCR=1,
languages Slovak and English) through its command line; PDF pages are rendered by PDFium
(pypdfium2), images are converted to PNG by Pillow. No extra Python package.
ocr_available() says whether this image can do OCR (/v1/info `ocr_available`; the API refuses
extraction.ocr = true with 400 invalid_options when it cannot).
"""
import functools
import io
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

WANTED_LANGUAGES = ("slk", "eng")
RENDER_DPI = 300  # Tesseract reads best at about 300 dpi
TIMEOUT_S = 120  # one page or image


class OcrError(RuntimeError):
    """Tesseract failed on a page or image."""


@functools.lru_cache(maxsize=1)
def ocr_languages() -> str:
    """'slk+eng' (the installed ones of WANTED_LANGUAGES), '' when Tesseract is not installed."""
    if shutil.which("tesseract") is None:
        return ""
    try:
        listed = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    installed = {line.strip() for line in listed.splitlines()[1:]}
    return "+".join(lang for lang in WANTED_LANGUAGES if lang in installed)


def ocr_available() -> bool:
    return bool(ocr_languages())


def ocr_image(image_bytes: bytes) -> str:
    """Text in an image (any format Pillow reads)."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            png = io.BytesIO()
            image.convert("RGB").save(png, format="PNG")
    except Exception as e:  # not an image Pillow can read (e.g. a vector format): nothing to read
        logger.info(f"[OCR] Image skipped ({type(e).__name__}).")
        return ""
    return _tesseract(png.getvalue())


def ocr_pdf_page(pdf_path: str, index: int) -> str:
    """Text of one PDF page (index 0 = page 1), rendered by PDFium."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_path)
    try:
        page = pdf[index]
        image = page.render(scale=RENDER_DPI / 72).to_pil()
        page.close()
    finally:
        pdf.close()
    png = io.BytesIO()
    image.save(png, format="PNG")
    return _tesseract(png.getvalue())


def _tesseract(png: bytes) -> str:
    languages = ocr_languages()
    if not languages:
        raise OcrError("OCR is not installed in this image.")
    try:
        result = subprocess.run(["tesseract", "stdin", "stdout", "-l", languages],
                                input=png, capture_output=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        raise OcrError(f"Tesseract failed: {e}") from e
    if result.returncode != 0:
        raise OcrError(f"Tesseract failed: {result.stderr.decode('utf-8', 'replace')[:200]}")
    return result.stdout.decode("utf-8", "replace").strip()
