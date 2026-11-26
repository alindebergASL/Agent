import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from work_agent.storage import Store
from work_agent.watch_subjects import observer
from work_agent.web import create_app


NOW = datetime(2026, 11, 25, 17, tzinfo=UTC)


class WatchSubjectTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.directory)
        self.store.initialize()
        self.task = self.store.submit("Save reporting guidance.", {"rules.txt": b"Use blue.\nUse green if approved."})
        self.store.claim()
        turn = self.store.get_run(self.task)["turn_id"]
        self.store.stage(self.task, "guidance.md", "Use blue.")
        self.store.stage_project_note(self.task, turn, "Reporting", 0,
                                      [{"text": "Use blue.", "sources": ["../uploads/rules.txt#L1"]}])
        self.store.complete(self.task, "Saved.")

    def create(self, subject):
        return self.store.create_watch("Track changes to the reporting guidance.", "daily", None, "15:00",
                                       now=NOW, subject=subject)

    def test_watch_starts_from_displayed_reference_and_observes_later_user_edit(self):
        with TestClient(create_app(self.directory, clock=lambda: NOW)) as client:
            page = f"/tasks/{self.task}/artifacts/guidance.md"
            params = {"source_task": self.task, "source_name": "guidance.md", "source_revision": "1"}
            self.assertIn("Watch this file from revision 1", client.get(page).text)
            form = client.get("/watches", params=params)
            self.assertIn("guidance.md · revision 1", form.text)
            self.store.save_markdown(self.task, "guidance.md", "Use green.", 1)
            posted = client.post("/watches", data={**params, "request": "Track revisions.", "frequency": "daily",
                                                   "local_time": "15:00"}, follow_redirects=False)
            self.assertEqual(posted.status_code, 303)
            self.assertIn("?revision=1", client.get(posted.headers["location"]).text)
        check = self.store.enqueue_watch_now(1, now=NOW)
        self.assertEqual(self.store.claim(), check)
        run = self.store.get_run(check)
        self.assertEqual(run["prior_artifacts"][0]["revision"], 1)
        subject = run["watch"]["subject"]
        observed = observer(self.store, subject)(1, 1)
        self.assertEqual(observed["lines"][0]["text"], "Use green.")
        self.assertTrue(observed["source_url"].endswith("?revision=2"))
        self.assertTrue(observed["starting_url"].endswith("?revision=1"))

    def test_note_preserves_starting_revision_and_exposes_current_claim_evidence(self):
        watch_id = self.create({"note_id": 1})
        self.store.edit_project_note(1, 1, [{"text": "Use green.", "supersedes": 1,
                                            "sources": [f"/tasks/{self.task}/uploads/rules.txt#L2"]}], "Approved.")
        reopened = Store(self.directory)
        reopened.initialize()
        subject = reopened.get_watch(watch_id)["subject"]
        page = observer(reopened, subject)(1, 1)
        self.assertEqual(page["starting_url"], "/notes/1?revision=1")
        self.assertEqual(page["source_url"], "/notes/1?revision=2")
        self.assertEqual(page["lines"][0]["claim_url"], "/notes/1?revision=2#claim-1")
        self.assertEqual(page["lines"][0]["evidence"], [f"/tasks/{self.task}/uploads/rules.txt#L2"])
        self.assertEqual(reopened.project_note_by_id(1, 1)["claims"][0]["text"], "Use blue.")

    def test_invalid_subject_rolls_back_entire_creation(self):
        for subject in ({"note_id": True}, {"note_id": 13}, {"source_task": self.task},
                        {"source_task": self.task, "source_name": "guidance.md", "source_revision": 2}):
            with self.subTest(subject=subject), self.assertRaises(ValueError):
                self.create(subject)
        self.assertEqual(self.store.list_watches(), [])

    def test_required_public_pages_follow_the_watch_into_each_check(self):
        fields = {"request": "Track policy. Ignore https://retired.example.org/rules.",
                  "frequency": "daily", "local_time": "16:00"}
        with TestClient(create_app(self.directory, clock=lambda: NOW)) as client:
            invalid = client.post("/watches", data={**fields, "required_urls": "javascript:void(0)"})
            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(self.store.list_watches(), [])
            posted = client.post("/watches", data={**fields, "required_urls":
                                 "https://EXAMPLE.org:443/rule#section\nhttps://example.org/other"},
                                 follow_redirects=False)
            self.assertEqual(posted.status_code, 303)
            self.assertIn("https://example.org/rule", client.get(posted.headers["location"]).text)
        urls = {"https://example.org/rule", "https://example.org/other"}
        self.assertEqual(set(self.store.get_watch(1)["required_urls"]), urls)
        check = self.store.enqueue_watch_now(1, now=NOW)
        self.store.claim()
        self.assertEqual(set(self.store.get_run(check)["watch"]["required_urls"]), urls)
