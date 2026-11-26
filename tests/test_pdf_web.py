"""Browser-facing upload, evidence preview and download for native PDFs."""

import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote
from unittest.mock import Mock

from fastapi.testclient import TestClient

from tests.pdf_fixture import make_pdf
from work_agent.storage import Store
from work_agent.web import MAX_FILE_BYTES, create_app


class PdfWebTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(folder)
        self.store.initialize()
        # The test controls turn transitions so that the HTTP paths can be exercised independently.
        self.client = TestClient(create_app(folder, Mock()))
        self.addCleanup(self.client.close)

    def test_pdf_original_selected_visual_and_cross_assignment_redirect(self):
        name = "R&D #2?.pdf"
        source = make_pdf(["Revision 1", "", "Section two\nA < B."])
        response = self.client.post("/tasks", data={"request": "Read this report."},
                                    files={"files": (name, source, "application/pdf")}, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        task_url = response.headers["location"]
        task = task_url.rsplit("/", 1)[1]
        url = task_url + "/uploads/" + quote(name, safe="")
        all_pages = self.client.get(url).text
        self.assertIn("View original page 3", all_pages)
        self.assertIn('id="L2">No extractable text on this page.', all_pages)
        page = self.client.get(url + "?page=3#L4")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Page 3 of 3", page.text)
        self.assertIn('<code>A &lt; B.</code>', page.text)
        self.assertIn('id="L4"', page.text)
        self.assertNotIn("Revision 1", page.text)
        self.assertIn('src="' + url + '/pages/3.png"', page.text)
        png = self.client.get(url + "/pages/3.png")
        self.assertEqual(png.headers["content-type"], "image/png")
        self.assertTrue(png.content.startswith(b"\x89PNG\r\n\x1a\n"))
        for suffix in ("?page=4", "/pages/4.png"):
            self.assertEqual(self.client.get(url + suffix).status_code, 404)
        original = self.client.get(url + "?download=1")
        self.assertEqual(original.content, source)
        self.assertTrue(original.headers["content-disposition"].startswith("attachment;"))
        self.assertEqual(original.headers["content-type"], "application/pdf")
        self.assertEqual(original.headers["x-content-type-options"], "nosniff")

        reused = self.client.post(url + "/new-assignment", data={"request": "Revisit the final page."},
                                  follow_redirects=False)
        self.assertEqual(reused.status_code, 303)
        other = reused.headers["location"].rsplit("/", 1)[1]
        prior = self.store.get_task(other)["prior_artifacts"][0]
        self.assertEqual(prior["source_task_id"], task)
        previous = self.client.get(f"/tasks/{other}/prior-artifacts/{prior['reference_id']}?page=3",
                                   follow_redirects=False)
        self.assertEqual(previous.headers["location"], url + "?page=3")

    def test_large_manual_opens_a_lightweight_line_index_and_only_the_selected_page(self):
        manual = make_pdf(["\n".join(f"Station {page} observation {row}" for row in range(40))
                           for page in range(1, 31)])
        task = self.store.submit("Keep this operations manual.", {"manual.pdf": manual})
        url = f"/tasks/{task}/uploads/manual.pdf"
        overview = self.client.get(url)
        self.assertEqual(overview.status_code, 200)
        self.assertIn('src="http://testserver/static/pdf-page-index.js"', overview.text)
        self.assertIn('data-first-line="1121" data-last-line="1160" href="?page=29"', overview.text)
        self.assertNotIn("Station 1 observation 0", overview.text)
        self.assertNotIn('class="source-lines"', overview.text)
        self.assertLess(len(overview.content), 16000)
        selected = self.client.get(url + "?page=29#L1121").text
        self.assertIn('id="L1121"', selected)
        self.assertIn("Station 29 observation 0", selected)
        self.assertNotIn("Station 30 observation 0", selected)
        self.assertNotIn('pdf-page-index.js', selected)

    def test_pdf_follow_up_only_text_reading_limit_is_unchanged(self):
        task = self.store.submit("Check the first memo.", {"memo.txt": b"Draft."})
        self.store.claim()
        self.store.complete(task, "Reviewed.")
        url = f"/tasks/{task}/follow-up"
        invalid = self.client.post(url, data={"request": "Read this."},
                                   files={"files": ("pending.pdf", b"%PDF-1.4\ninvalid")})
        self.assertEqual(invalid.status_code, 400)
        self.assertIsNone(self.store.read_upload(task, "pending.pdf"))
        large_text = self.client.post(url, data={"request": "Read this."},
                                      files={"files": ("oversized.txt", b"x" * (MAX_FILE_BYTES + 1))})
        self.assertEqual(large_text.status_code, 400)
        self.assertIn("128 KiB", large_text.text)
        source = make_pdf(["Approved rule"])
        ok = self.client.post(url, data={"request": "Review the amendment."},
                              files={"files": ("amendment.pdf", source, "application/octet-stream")},
                              follow_redirects=False)
        self.assertEqual(ok.status_code, 303)
        self.assertEqual(self.store.read_upload(task, "amendment.pdf"), source)
