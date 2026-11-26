"""Extract searchable text and render physical pages from an original PDF using Poppler."""

import re
import subprocess
from dataclasses import dataclass
from tempfile import TemporaryFile
from threading import BoundedSemaphore


MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_LINES = 50_000
MAX_PREVIEW_BYTES = 6 * 1024 * 1024
# Applied independently to each Poppler process. File-backed output lets the OS
# bound writes before Python reads any result. No preexec_fn in the threaded app.
_MEMORY_BYTES = 384 * 1024 * 1024
_CPU_SECONDS = 10
_TIMEOUT_SECONDS = 15
_PARSER_SLOTS = BoundedSemaphore(2)


class PdfError(ValueError):
    """The supplied PDF or requested page could not be safely processed."""


@dataclass(frozen=True)
class PdfPage:
    number: int
    start_line: int
    end_line: int
    text: str

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())


@dataclass(frozen=True)
class PdfDocument:
    text: str
    pages: tuple[PdfPage, ...]


def is_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF-")


def _validate_input(data):
    if len(data) > MAX_PDF_BYTES:
        raise PdfError("Upload a PDF of 16 MiB or smaller.")
    if not is_pdf(data):
        raise PdfError("This file has no PDF header. Supply the original PDF.")


def _run(args, data, max_output):
    if not _PARSER_SLOTS.acquire(timeout=_TIMEOUT_SECONDS):
        raise PdfError("The PDF reader is busy. Try again shortly.")
    try:
        with TemporaryFile() as output, TemporaryFile() as errors:
            command = ["/usr/bin/prlimit", f"--as={_MEMORY_BYTES}", f"--cpu={_CPU_SECONDS}",
                       f"--fsize={max_output}", "--core=0", "--", *args]
            result = subprocess.run(command, input=data, stdout=output, stderr=errors, timeout=_TIMEOUT_SECONDS,
                                    env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": "/nonexistent"})
            if output.tell() >= max_output or errors.tell() >= max_output:
                raise PdfError("PDF processing exceeded the output limit. Split the document and try again.")
            errors.seek(0)
            diagnostics = errors.read()
            if result.returncode:
                if b"password" in diagnostics[:4096].lower():
                    raise PdfError("Unlock the password-protected PDF before uploading it.")
                raise PdfError("The PDF could not be processed within the reader limits. Check the file or split it.")
            # Poppler can return success while skipping corrupt page operators.
            if re.search(rb"^Syntax Error\b", diagnostics, re.MULTILINE):
                raise PdfError("The PDF contains unreadable data. Re-export it and try again.")
            output.seek(0)
            return output.read()
    except subprocess.TimeoutExpired:
        raise PdfError("The PDF took too long to process. Split the document and try again.") from None
    except FileNotFoundError:
        raise PdfError("The PDF reader is unavailable. Install poppler-utils and util-linux in the app image.") from None
    finally:
        _PARSER_SLOTS.release()


def extract_pdf(data: bytes) -> PdfDocument:
    _validate_input(data)
    info = _run(["/usr/bin/pdfinfo", "-"], data, 256 * 1024)
    counts = re.findall(rb"^Pages:[ \t]+([0-9]+)[ \t]*$", info, re.MULTILINE)
    if not counts or len(counts[-1]) > 4 or not 1 <= int(counts[-1]) <= MAX_PDF_PAGES:
        raise PdfError(f"PDFs must have between 1 and {MAX_PDF_PAGES} pages.")
    encryption = re.findall(rb"^Encrypted:[ \t]+(yes|no)\b", info, re.MULTILINE)
    if encryption and encryption[-1] == b"yes":
        raise PdfError("Unlock the encrypted PDF before uploading it.")

    output = _run(["/usr/bin/pdftotext", "-layout", "-enc", "UTF-8", "-eol", "unix", "-", "-"], data, MAX_TEXT_BYTES + 1)
    try:
        page_texts = output.decode("utf-8").split("\f")
    except UnicodeDecodeError:
        raise PdfError("The PDF contains invalid character mappings. Re-export it with searchable text.") from None
    if b"\x00" in output:
        raise PdfError("The PDF contains unreadable text data. Re-export it with searchable text.")
    if page_texts.pop() != "" or len(page_texts) != int(counts[-1]):
        raise PdfError("Could not map extracted text to the original PDF pages.")
    pages = []
    next_line = 1
    for number, raw in enumerate(page_texts, 1):
        # Normalize all breaks using splitlines, the same rule used by read_file.
        # Keep indentation for table columns. An empty page owns one blank line.
        lines = [line.rstrip() for line in raw.strip("\n").splitlines()]
        if not any(lines):
            lines = [""]
        end_line = next_line + len(lines) - 1
        if end_line > MAX_LINES:
            raise PdfError(f"Extracted PDF text exceeds {MAX_LINES:,} lines. Split the document and try again.")
        pages.append(PdfPage(number, next_line, end_line, "\n".join(lines)))
        next_line = end_line + 1
    if not any(page.has_text for page in pages):
        raise PdfError("No readable PDF text was found. Upload a searchable or OCR-processed copy.")
    return PdfDocument("".join(page.text + "\n" for page in pages), tuple(pages))


def render_page(data: bytes, page: int) -> bytes:
    _validate_input(data)
    if type(page) is not int or not 1 <= page <= MAX_PDF_PAGES:
        raise PdfError("Choose a physical page number in this PDF.")
    png = _run(["/usr/bin/pdftoppm", "-png", "-singlefile", "-scale-to", "1280", "-f", str(page), "-l",
                str(page), "-"], data, MAX_PREVIEW_BYTES)
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PdfError("Could not render this PDF page.")
    return png
