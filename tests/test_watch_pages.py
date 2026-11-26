import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from work_agent.storage import Store
from work_agent.web import create_app


NOW = datetime(2026, 11, 25, 19, tzinfo=timezone.utc)


class WatchPageTests(unittest.TestCase):
    def setUp(self):
        work = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(Path(work))
        self.enterContext(patch.dict("os.environ", {"OPENAI_API_KEY": ""}))
        self.client = self.enterContext(TestClient(create_app(Path(work), clock=lambda: NOW)))

    def test_multiple_cadences_check_now_and_stop_independent_of_weekly_responsibility(self):
        weekly = self.store.create_schedule("Publish a research brief.", 1, "15:25", now=NOW)
        data = {"request": "Tell me when public nominations open.", "local_time": "10:00", "frequency": "daily"}
        self.assertIn("Watches</a>", self.client.get("/").text)
        first = self.client.post("/watches", data=data)
        self.assertEqual(first.status_code, 200)
        self.assertIn("Checks every day", first.text)
        self.assertIn("Check now</button>", first.text)
        second = self.client.post("/watches", data={**data, "request": "Report adopted amendments.", "frequency": "4"})
        self.assertIn("Checks every Friday", second.text)
        self.assertIn("Tell me when public nominations open.", self.client.get("/watches").text)
        self.assertEqual(self.client.post(first.url.path + "/check-now").status_code, 503)
        self.assertEqual(self.client.post(first.url.path + "/stop", headers={"Origin": "https://other.example"}).status_code, 403)
        stopped = self.client.post(first.url.path + "/stop")
        self.assertIn("Stopped Wednesday", stopped.text)
        self.assertNotIn("Stop watch</button>", stopped.text)
        self.assertIn("Stop watch</button>", self.client.get(second.url.path).text)
        self.assertEqual(self.store.get_schedule()["id"], weekly)
        self.assertIsNone(self.store.get_schedule()["stopped_at"])
        invalid = self.client.post("/watches", data={**data, "frequency": "8"})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn(data["request"], invalid.text)
        self.assertEqual(len(self.store.list_watches()), 2)

    def test_acknowledgement_is_scoped_and_keeps_finding_in_source_linked_history(self):
        watch = self.store.create_watch("Tell me when funding opens.", "daily", None, "10:00", now=NOW)
        other = self.store.create_watch("Watch approvals.", "daily", None, "11:00", now=NOW)
        task = self.store.enqueue_watch_now(watch, now=NOW)
        self.assertEqual(self.store.claim(), task)
        turn = self.store.get_run(task)["turn_id"]
        url = "https://example.org/program/open"
        finding = {"key": "open-1", "title": "The research call opened", "why": "Applications are due Monday.",
                   "evidence": [url]}
        self.store.stage_watch_assessment(task, turn, outcome="material", summary="Applications opened.",
                                          findings=[finding], evidence=[url])
        self.store.complete(task, "Saved.", turn_id=turn)
        notice = self.store.watch_attention()[0]
        home = self.client.get("/")
        self.assertIn("Needs your attention", home.text)
        self.assertIn(f"/watches/{watch}#check-{task}", home.text)
        self.assertIn(f'href="{url}"', home.text)
        suffix = f"/notices/{notice['id']}/acknowledge"
        self.assertEqual(self.client.post(f"/watches/{other}" + suffix).status_code, 404)
        self.assertIn(finding["title"], self.client.get("/").text)
        reviewed = self.client.post(f"/watches/{watch}" + suffix)
        self.assertNotIn("Needs your attention", reviewed.text)
        detail = self.client.get(f"/watches/{watch}")
        self.assertIn(f'id="check-{task}"', detail.text)
        self.assertIn(finding["title"], detail.text)
        self.assertIn(f'href="{url}"', detail.text)
        self.assertIn(f"/watches/{watch}#check-{task}", self.client.get(f"/tasks/{task}").text)
