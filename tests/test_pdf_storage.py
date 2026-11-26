import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.pdf_fixture import make_pdf
from work_agent.storage import Store


class PdfStorageTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(root)
        self.store.initialize()

    def test_physical_pages_preserved_across_search_pin_and_restart(self):
        pages = ["Requests require approval.\nLimit: 25 units.", "", "Only sealed items qualify."]
        original = make_pdf(pages)
        task = self.store.submit("Review the policy.", {"guide.pdf": original})
        other = self.store.submit("Review the other guide.", {"guide.pdf": make_pdf(["Use the red dock."])})
        target = self.store.submit("Revisit the qualification.", {})
        with patch("work_agent.pdf_documents.extract_pdf", side_effect=AssertionError("PDF reparsed")):
            self.store.initialize()
            matches = self.store.search_saved_work(target, "sealed items")
            self.assertEqual(matches[0]["source_task_id"], task)
            self.assertEqual(matches[0]["passage"]["page"], 3)
            self.assertNotIn("25 units", matches[0]["passage"]["text"])
            self.assertNotIn(other, [match["source_task_id"] for match in matches])
            pin = self.store.pin_saved_work(target, task, "guide.pdf", None)
            text, layout = self.store.read_upload_projection(task, "guide.pdf")
            self.assertEqual([p["has_text"] for p in layout], [True, False, True])
            self.assertEqual(text.splitlines(), [*pages[0].splitlines(), "", pages[2]])
            self.assertEqual(pin["pdf_pages"], layout)
            self.assertEqual(self.store.pdf_layouts(task), {"guide.pdf": layout})
            self.assertEqual(self.store.read_upload(task, "guide.pdf"), original)
            self.assertEqual(self.store.read_prior_artifact(target, pin["reference_id"]), original)
            self.assertEqual(self.store.read_prior_text(target, pin["reference_id"]).decode(), text)
            self.assertEqual(self.store.read_uploads(task), {"guide.pdf": text})

    def test_search_within_one_pdf_preserves_all_physical_occurrences_and_bounds_results(self):
        pages = ["Carbon reporting applies to area " + str(i) + "." for i in range(1, 12)]
        task = self.store.submit("Which areas are covered?", {"audit.pdf": make_pdf(pages),
                                                               "public.txt": b"Carbon reporting"})
        other = self.store.submit("Different audit.", {"audit.pdf": make_pdf(["Carbon reporting at East."])})
        first = self.store.search_pdf_pages(task, "audit.pdf", "carbon reporting")
        second = self.store.search_pdf_pages(task, "audit.pdf", "carbon reporting", first["next_offset"])
        self.assertEqual([hit["page"] for hit in first["matches"]], list(range(1, 9)))
        self.assertEqual([hit["page"] for hit in second["matches"]], [9, 10, 11])
        self.assertEqual(second["matches"][-1]["text"], pages[-1])
        self.assertIsNone(second["next_offset"])
        east = self.store.search_pdf_pages(other, "audit.pdf", "East")
        self.assertEqual(east["matches"][0]["page"], 1)
        self.assertFalse(self.store.search_pdf_pages(task, "audit.pdf", "East")["matches"])
        for name, query, offset in (("public.txt", "carbon", 0), ("audit.pdf", "!!", 0),
                                    ("audit.pdf", "carbon", True), ("audit.pdf", "carbon", -1)):
            with self.subTest(name=name, query=query, offset=offset), self.assertRaises(ValueError):
                self.store.search_pdf_pages(task, name, query, offset)

    def test_invalid_pdf_never_partially_attaches_or_resumes_waiting_turn(self):
        task = self.store.submit("Check eligibility.", {"record.txt": b"Pending\r\n"})
        self.store.claim()
        turn = self.store.get_run(task)["turn_id"]
        self.store.wait_for_input(task, "Supply the current bulletin.", turn_id=turn)
        with self.assertRaises(ValueError):
            self.store.answer(task, turn, "Use these.", {"valid.pdf": make_pdf(["Docks 1 and 2."]), "bad.pdf": b"damaged"})
        self.assertEqual(self.store.get_task(task)["status"], "waiting")
        self.assertEqual(self.store.read_uploads(task), {"record.txt": "Pending\r\n"})
        self.store.answer(task, turn, "Attached.", {"valid.pdf": make_pdf(["Use dock 3."])})
        self.assertEqual(self.store.read_uploads(task)["valid.pdf"].strip(), "Use dock 3.")
        self.store.claim()
        self.store.complete(task, "Confirmed.")
        self.store.follow_up(task, "Apply the addendum.", {"amendment.pdf": make_pdf(["Use dock 4."])})
        self.assertEqual(self.store.read_upload_text(task, "amendment.pdf").decode().strip(), "Use dock 4.")
