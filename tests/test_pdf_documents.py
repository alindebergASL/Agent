import struct
import unittest
from unittest.mock import patch

from tests.pdf_fixture import make_pdf
from work_agent import pdf_documents


class PdfDocumentTests(unittest.TestCase):
    def test_physical_page_to_line_mapping_including_empty_pages(self):
        data = make_pdf(["", "First heading\nTerms (in force).", "", "Café status", ""])
        doc = pdf_documents.extract_pdf(data)
        self.assertEqual(doc.text.splitlines(), ["", "First heading", "Terms (in force).", "", "Café status", ""])
        self.assertEqual([p.number for p in doc.pages], [1, 2, 3, 4, 5])
        self.assertEqual([(p.start_line, p.end_line) for p in doc.pages], [(1, 1), (2, 3), (4, 4), (5, 5), (6, 6)])
        self.assertEqual([p.number for p in doc.pages if not p.has_text], [1, 3, 5])

    def test_selected_page_renders_as_png_with_original_dimensions_ratio(self):
        data = make_pdf(["First source", "Second source"])
        first, second = [pdf_documents.render_page(data, page) for page in (1, 2)]
        self.assertTrue(first.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack(">II", first[16:24])
        self.assertEqual(height, 1280)
        self.assertAlmostEqual(width / height, 612 / 792, places=2)
        self.assertNotEqual(first, second)

    def test_invalid_scanned_encrypted_and_excessive_pages_have_actionable_errors(self):
        for data, message in ((b"hello", "header"), (b"%PDF-1.4\ninvalid", "could not be processed"),
                              (make_pdf([""]), "OCR"), (make_pdf([""] * 501), "500")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                pdf_documents.extract_pdf(data)
        with patch.object(pdf_documents, "_run", return_value=b"Pages: 1\nEncrypted: yes\n"):
            with self.assertRaisesRegex(ValueError, "Unlock"):
                pdf_documents.extract_pdf(make_pdf(["A"]))

    def test_limits_reject_oversized_input_and_text_instead_of_publishing_a_partial_page(self):
        data = make_pdf(["Long enough to hit a reduced bound."])
        with patch.object(pdf_documents, "MAX_PDF_BYTES", len(data) - 1), self.assertRaisesRegex(ValueError, "MiB"):
            pdf_documents.extract_pdf(data)
        with patch.object(pdf_documents, "MAX_TEXT_BYTES", 6), self.assertRaisesRegex(ValueError, "output limit"):
            pdf_documents.extract_pdf(data)
        with patch.object(pdf_documents, "MAX_LINES", 1), self.assertRaisesRegex(ValueError, "lines"):
            pdf_documents.extract_pdf(make_pdf(["A", "B"]))

    def test_counts_must_match_before_original_page_is_attributed(self):
        with patch.object(pdf_documents, "_run", side_effect=[b"Pages: 3\n", b"text\n\f"]):
            with self.assertRaisesRegex(ValueError, "map extracted text"):
                pdf_documents.extract_pdf(make_pdf(["A"]))

    def test_partial_text_after_a_page_syntax_error_is_rejected(self):
        malformed = make_pdf(["Some words still extract"]).replace(b"BT /F1", b"BZ /F1")
        with self.assertRaisesRegex(ValueError, "unreadable"):
            pdf_documents.extract_pdf(malformed)
