import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock

from fastapi.testclient import TestClient

from tests.quote_fixture import answer
from tests.test_connected_workflow import Links
from work_agent.storage import Store
from work_agent.web import create_app


class ReuseRevisionTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.data)
        self.store.initialize()
        self.source = self.store.submit("Draft meeting notes", {})
        self.store.claim()
        self.store.stage(self.source, "notes.md", "Original notes")
        self.store.complete(self.source, "Done")

    def test_start_assignment_with_displayed_revision_even_if_edited_before_submit(self):
        received = []
        started = Event()

        def respond(history, tools, instructions, *, max_tool_calls):
            received.append(json.loads(next(item["content"] for item in history if item.get("role") == "user")))
            started.set()
            return {"status": "completed", "output": [answer("Done")]}

        model = Mock()
        model.respond.side_effect = respond
        with TestClient(create_app(data_dir=self.data, model=model)) as client:
            artifact = f"/tasks/{self.source}/artifacts/notes.md"
            links = Links(client.get(artifact).text).hrefs
            use_revision, = [link for link in links if link.endswith("/revisions/1/new-assignment")]
            form = client.get(use_revision)
            self.assertEqual(form.status_code, 200)
            self.assertIn(f'action="{use_revision}"', form.text)
            self.store.save_markdown(self.source, "notes.md", "Newer notes", 1)
            invalid = client.post(use_revision, data={"request": ""})
            self.assertEqual(invalid.status_code, 400)
            self.assertIn(f'action="{use_revision}"', invalid.text)
            result = client.post(use_revision, data={"request": "Summarize the selected notes."}, follow_redirects=False)
            self.assertEqual(result.status_code, 303)
            self.assertTrue(started.wait(timeout=5))
            prior, = received[0]["prior_artifacts"]
            self.assertEqual((prior["source_task_id"], prior["name"], prior["revision"]), (self.source, "notes.md", 1))
            linked = client.get(result.headers["location"] + f"/prior-artifacts/{prior['reference_id']}")
            self.assertEqual(str(linked.url), f"http://testserver{artifact}?revision=1")
            self.assertEqual(client.get(str(linked.url) + "&download=1").content, b"Original notes")
            self.assertEqual(self.store.read_artifact(self.source, "notes.md"), b"Newer notes")

    def test_invalid_revision_creates_no_assignment(self):
        with TestClient(create_app(data_dir=self.data, model=Mock())) as client:
            missing = f"/tasks/{self.source}/artifacts/notes.md/revisions/999/new-assignment"
            self.assertEqual(client.get(missing).status_code, 404)
            self.assertEqual(client.post(missing, data={"request": "Use it"}).status_code, 400)
            self.assertEqual([task["id"] for task in self.store.list_tasks()], [self.source])
