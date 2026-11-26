"""Freeze supported Office content into one independently citable location per line."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

FORMATS = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
MAX_BYTES = 16 * 1024 * 1024


def kind(name):
    return Path(name).suffix.lower() if Path(name).suffix.lower() in FORMATS else None


def workbook_rows(data):
    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(data), read_only=False, keep_links=False)
    budget = 500_000
    for sheet in book.worksheets:
        budget -= sheet.max_column * sheet.max_row
        if budget < 0:
            raise ValueError("Workbook used ranges are too large. Export the relevant sheets.")
        for row in sheet.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                address = "'" + sheet.title.replace("'", "''") + "'!" + cell.coordinate
                label = {"f": "formula (unevaluated)", "n": "number", "e": "cell error",
                         "b": "boolean", "d": "date/time"}.get(cell.data_type, "text")
                value = cell.value
                if cell.data_type == "f" and not isinstance(value, str):
                    value = getattr(value, "text", None) or "[complex formula]"
                if cell.number_format != "General":
                    address += f" [format: {cell.number_format}]"
                if sheet.sheet_state != "visible":
                    address += " (hidden sheet)"
                yield address, label, str(value)


def word_rows(data):
    from docx import Document
    from docx.text.paragraph import Paragraph
    p = t = 0
    for block in Document(io.BytesIO(data)).iter_inner_content():
        if isinstance(block, Paragraph):
            p += 1
            if block.text.strip():
                yield f"Paragraph {p}", "text", block.text
        else:
            t += 1
            for r, row in enumerate(block.rows, 1):
                for c, cell in enumerate(row.cells, row.grid_cols_before + 1):
                    if cell.tables:
                        raise ValueError("This Word table contains nested tables. Supply a flattened copy.")
                    if cell.text.strip():
                        yield f"Table {t}, row {r}, column {c}", "text", cell.text


def project(data, ext):
    with ZipFile(io.BytesIO(data)) as package:
        parts = package.infolist()
        if len(parts) > 5000 or sum(p.file_size for p in parts) > 64 * 1024 * 1024:
            raise ValueError("Office document exceeds extraction limits.")
        if len({p.filename for p in parts}) != len(parts) or any(p.flag_bits & 1 for p in parts):
            raise ValueError("Use an unencrypted Office document with unique parts.")
        if any(p.filename.lower().endswith("vbaproject.bin") for p in parts):
            raise ValueError("Export a macro-free copy.")
    rows = workbook_rows(data) if ext == ".xlsx" else word_rows(data)
    size = 0
    lines = []
    for address, label, value in rows:
        # A literal tab separates fields. Escaped values preserve cell and paragraph boundaries.
        fields = [json.dumps(field, ensure_ascii=False)[1:-1] for field in (address, label, value)]
        line = "\t".join(fields)
        for separator in ("\u0085", "\u2028", "\u2029", "\x7f"):
            line = line.replace(separator, ascii(separator)[1:-1])
        size += len(line.encode()) + 1
        if len(line) > 16000 or len(lines) >= 50000 or size > 4 * 1024 * 1024:
            raise ValueError("Office text exceeds the reader limits. Split the document.")
        lines.append(line)
    if not lines:
        raise ValueError("No supported text or cells were found.")
    return "\n".join(lines)


def extract(data, ext):
    if len(data) > MAX_BYTES:
        raise ValueError("Office uploads may be up to 16 MiB.")
    try:
        result = subprocess.run(["/usr/bin/prlimit", "--as=536870912", "--cpu=12", "--core=0", "--",
                                 sys.executable, "-m", "work_agent.office_documents", ext],
                                input=data, capture_output=True, timeout=18,
                                env={"PATH": os.defpath, "LC_ALL": "C.UTF-8", "OPENBLAS_NUM_THREADS": "1"})
    except subprocess.TimeoutExpired:
        raise ValueError("Office extraction timed out. Split the document.") from None
    if result.returncode:
        raise ValueError("Could not read this Office document. Use a supported, unencrypted file within the reader limits.")
    return result.stdout.decode("utf-8")


if __name__ == "__main__":
    try:
        sys.stdout.buffer.write(project(sys.stdin.buffer.read(MAX_BYTES + 1), sys.argv[1]).encode())
    except Exception:
        sys.exit(1)
