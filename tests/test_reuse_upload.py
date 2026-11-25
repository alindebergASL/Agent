import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import quote

from fastapi.testclient import TestClient

from tests.quote_fixture import answer
from tests.test_connected_workflow import Links
from work_agent.storage import Store
from work_agent.web import create_app


class ReuseUploadTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.data)
        self.store.initialize()
        self.name = "Café #7? 100%"
        self.original = b"part,qty\r\n000731,120\r\n"
        self.source = self.store.submit("Summarize these parts", {
            self.name: self.original, "summary.csv": b"source\n123\n",
        })
        self.store.claim()
        self.store.stage(self.source, "summary.csv", "output\n456\n")
        self.store.complete(self.source, "Done")
        self.model = Mock()
        self.model.respond.return_value = {"status": "completed", "output": [answer("Done")]}

    def test_manual_original_opens_exact_rows_and_bytes_after_restart(self):
        source_page = f"http://testserver/tasks/{self.source}"
        original_url = f"{source_page}/uploads/{quote(self.name, safe='')}"
        with TestClient(create_app(self.data, self.model)) as client:
            self.assertIn(original_url, Links(client.get(source_page).text).hrefs)
            original = client.get(original_url)
            form_url, = [link for link in Links(original.text).hrefs if link.endswith("/new-assignment")]
            form = client.get(form_url)
            self.assertIn(f'action="{form_url}"', form.text)
            self.assertIn(original_url, Links(form.text).hrefs)
            error = client.post(form_url, data={"request": ""})
            self.assertEqual(error.status_code, 400)
            self.assertIn(f'action="{form_url}"', error.text)
            created = client.post(form_url, data={"request": "Use the original"}, follow_redirects=False)
            self.assertEqual(created.status_code, 303)
            task_url = created.headers["location"]
            task = self.store.get_task(task_url.rsplit("/", 1)[-1])
            self.assertEqual(task["uploads"], [])
            prior, = task["prior_artifacts"]
            alias = f"{task_url}/prior-artifacts/{prior['reference_id']}"
            self.assertIn(alias, Links(client.get(task_url).text).hrefs)

        with TestClient(create_app(self.data, self.model)) as client:
            redirect = client.get(alias, follow_redirects=False)
            self.assertEqual(redirect.headers["location"], original_url)
            source = client.get(alias + "#L2")
            self.assertIn('id="L2"', source.text)
            self.assertIn("000731,120", source.text)
            self.assertEqual(client.get(original_url + "?download=1").content, self.original)
            self.assertEqual(client.get(f"{source_page}/prior-artifacts/{prior['reference_id']}").status_code, 404)

    def test_same_named_output_and_upload_resolve_in_separate_source_routes(self):
        source_page = f"http://testserver/tasks/{self.source}"
        with TestClient(create_app(self.data, self.model)) as client:
            for path, query, content in (
                ("uploads/summary.csv", "", b"source\n123\n"),
                ("artifacts/summary.csv", "?revision=1", b"output\n456\n"),
            ):
                with self.subTest(path=path):
                    source_url = f"{source_page}/{path}"
                    form_url, = [link for link in Links(client.get(source_url).text).hrefs
                                 if link.endswith("/new-assignment")]
                    task = client.post(form_url, data={"request": "Summarize the selected source"})
                    alias, = [link for link in Links(task.text).hrefs if "/prior-artifacts/" in link]
                    selected = client.get(alias)
                    self.assertEqual(str(selected.url), source_url + query)
                    self.assertEqual(client.get(selected.url.copy_add_param("download", "1")).content, content)

            before = len(self.store.list_tasks())
            missing = f"{source_page}/uploads/missing/new-assignment"
            self.assertEqual(client.get(missing).status_code, 404)
            self.assertEqual(client.post(missing, data={"request": "Use it"}).status_code, 400)
            self.assertEqual(len(self.store.list_tasks()), before)
