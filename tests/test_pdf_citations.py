import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tests.pdf_fixture import make_pdf
from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import run_assignment
from work_agent.storage import Store


class PdfCitationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.store.initialize()

    def test_jump_to_page_and_search_bounded_current_and_pinned_sources(self):
        name = "cost#?.pdf"
        earlier = self.store.submit("Archive observations.", {name: make_pdf(["Earlier rack", "Observed cost $40"])})
        self.store.claim()
        self.store.complete(earlier, "Archived.")
        current = self.store.submit("Read page 73 and check the earlier cost.", {
            name: make_pdf([f"Page marker {n}" for n in range(1, 73)] + ["Observed cost $35"])},
            prior_revision=(earlier, name, None))
        self.store.claim()
        run = self.store.get_run(current)
        pin = run["prior_artifacts"][0]["reference_id"]
        original = f"/tasks/{earlier}/uploads/cost%23%3F.pdf?page=2#L2"
        model = Mock()
        model.respond.side_effect = [
            {"status": "completed", "output": [
                call("locate", "locate_pdf_page", source=name, page=73),
                call("old_search", "search_pdf_pages", source=pin, query="observed", offset=0),
                call("bad_ref", "locate_pdf_page", source=pin + 1, page=1)]},
            {"status": "completed", "output": [
                call("line", "read_file", file_id=name, start_line=73, line_count=1),
                call("lookup", "read_cited_evidence", source_url=original, start_line=0, line_count=1)]},
            {"status": "completed", "output": [answer("Checked the two observations.")]},
        ]
        steps, progress = [], []
        run_assignment(model, "Compare page 73 with the previous result.", self.store.read_uploads(current),
                       Mock(), progress.append, steps.append, prior_artifacts=run["prior_artifacts"],
                       read_prior_artifact=lambda ref: self.store.read_prior_text(current, ref),
                       project_memory={"claims": [{"sources": [original]}]}, task_id=current,
                       project_note_store=self.store, stage_project_note=Mock(), pdf_layouts=self.store.pdf_layouts(current),
                       search_pdf_pages=self.store.search_pdf_pages,
                       pin_saved_work=lambda task, file, rev: self.store.pin_saved_work(current, task, file, rev))
        request = json.loads(model.respond.call_args_list[0].args[0][0]["content"])
        self.assertEqual(request["pdf_documents"][name], {"page_count": 73})
        self.assertNotIn("pdf_pages", request["prior_artifacts"][0])
        receipts = tool_outputs(steps)
        self.assertEqual(receipts["locate"]["start_line"], 73)
        self.assertEqual([hit["page"] for hit in receipts["old_search"]["matches"]], [2])
        self.assertIn("error", receipts["bad_ref"])
        self.assertEqual(receipts["line"]["lines"][0]["evidence_url"], "../uploads/cost%23%3F.pdf?page=73#L73")
        self.assertEqual(receipts["lookup"]["lines"], [{"number": 2, "text": "Observed cost $40", "page": 2,
                                                        "evidence_url": original}])
        self.assertIn("Running tool locate_pdf_page.", progress)
        self.assertIn("Running tool search_pdf_pages.", progress)

    def test_only_matching_physical_pages_and_readable_lines_support_evidence(self):
        task = self.store.submit("Compare", {"form.pdf": make_pdf(["one", "", "three"]), "notes.txt": b"one\n"})
        self.store.claim()
        base = "../uploads/form.pdf"
        for invalid in (f"{base}?page=1#L3", f"{base}?page=2#L2", f"{base}#L2", f"{base}?page=9#L9",
                        "../uploads/notes.txt?page=1#L1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.resolve_note_source(task, invalid)
        valid = f"{base}?page=3#L3"
        canon = f"/tasks/{task}/uploads/form.pdf?page=3#L3"
        self.assertEqual(self.store.resolve_note_source(task, f"{base}#L3"), canon)
        self.store.stage(task, "findings.md", f"[one]({base}?page=3#L1) [blank]({base}#L2) [three]({valid})")
        note = [{"text": "three", "sources": [valid]}]
        self.store.stage_project_note(task, self.store.get_run(task)["turn_id"], "Facility", 0, note)
        self.store.complete(task, "Done")
        uses = self.store.evidence_uses(task, "form.pdf", None)
        self.assertEqual(uses["citing_revisions"][0]["lines"], [3])
        self.assertEqual(uses["note_claims"][0]["claim"]["sources"], [canon])
