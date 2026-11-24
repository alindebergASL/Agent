import json
import re
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

from fastapi.testclient import TestClient

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.storage import Store
from work_agent.web import create_app


REQUEST = "Prepare a short meeting brief on the University of Iowa's published AI resources."
THURSDAY = datetime(2026, 10, 29, 16, tzinfo=UTC)
FRIDAY_PDT = datetime(2026, 10, 30, 16, tzinfo=UTC)
FRIDAY_PST = datetime(2026, 11, 6, 17, tzinfo=UTC)


class BriefModel:
    def __init__(self):
        self.requests = []

    def respond(self, history, tools, instructions, *, max_tool_calls):
        if "write-brief" in tool_outputs(history):
            output = [answer("Saved the dated meeting brief.")]
        else:
            user = next(item for item in reversed(history) if item.get("role") == "user")
            request = json.loads(user["content"])
            self.requests.append(request)
            date = re.search(r"Use (\d{4}-\d{2}-\d{2}) Pacific as the run date", request["request"]).group(1)
            output = [call(
                "write-brief", "write_artifact", name=f"meeting-brief-{date}.md",
                content=f"# Meeting brief — {date}\n\nReview official AI resources.\n",
            )]
        return {"status": "completed", "output": output}


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.data = Path(directory)

    def friday_schedule(self):
        store = Store(self.data)
        store.initialize()
        store.create_schedule(REQUEST, 4, "09:00", now=THURSDAY)
        return store

    def test_pacific_wall_clock_survives_dst_and_does_not_enqueue_early(self):
        store = self.friday_schedule()
        original = store.get_schedule()
        self.assertEqual(original["next_run_at"], int(FRIDAY_PDT.timestamp()))
        self.assertIsNone(store.enqueue_due_schedule(now=FRIDAY_PDT - timedelta(seconds=1)))
        self.assertEqual(store.list_tasks(), [])

        reopened = Store(self.data)
        reopened.initialize()
        self.assertEqual(reopened.get_schedule(), original)
        first = reopened.enqueue_due_schedule(now=FRIDAY_PDT)
        self.assertIsNotNone(first)
        self.assertIsNone(reopened.enqueue_due_schedule(now=FRIDAY_PDT))
        schedule = reopened.get_schedule()
        self.assertEqual(schedule["latest_run"], {
            "task_id": first, "due_at": int(FRIDAY_PDT.timestamp()), "status": "queued",
        })
        self.assertEqual(schedule["next_run_at"], int(FRIDAY_PST.timestamp()))
        self.assertIsNone(reopened.enqueue_due_schedule(now=FRIDAY_PST - timedelta(hours=1)))
        second = reopened.enqueue_due_schedule(now=FRIDAY_PST)
        self.assertIsNotNone(second)
        self.assertNotEqual(second, first)
        self.assertEqual({task["id"] for task in reopened.list_tasks()}, {first, second})

    def test_missed_occurrences_coalesce_once_and_stop_prevents_future_work(self):
        store = self.friday_schedule()
        peer = Store(self.data)
        peer.initialize()
        now = datetime(2026, 11, 22, 18, tzinfo=UTC)
        latest_due = datetime(2026, 11, 20, 17, tzinfo=UTC)
        next_due = datetime(2026, 11, 27, 17, tzinfo=UTC)
        barrier = Barrier(2)

        def check(database):
            barrier.wait(timeout=5)
            return database.enqueue_due_schedule(now=now)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(check, (store, peer)))
        self.assertEqual(results.count(None), 1)
        created, = (task_id for task_id in results if task_id is not None)
        task, = store.list_tasks()
        self.assertEqual(task["id"], created)
        self.assertIn("Friday, November 20, 2026 at 9:00 AM PST", task["request"])
        self.assertIn("Use 2026-11-22 Pacific as the run date", task["request"])
        schedule = store.get_schedule()
        self.assertEqual(schedule["latest_run"]["due_at"], int(latest_due.timestamp()))
        self.assertEqual(schedule["next_run_at"], int(next_due.timestamp()))

        reopened = Store(self.data)
        reopened.initialize()
        self.assertIsNone(reopened.enqueue_due_schedule(now=now))
        self.assertEqual(reopened.get_schedule()["latest_run"]["task_id"], created)
        reopened.stop_schedule(schedule["id"], now=now)
        self.assertEqual(store.get_schedule()["stopped_at"], int(now.timestamp()))
        self.assertIsNone(store.enqueue_due_schedule(now=next_due + timedelta(weeks=2)))
        self.assertEqual([task["id"] for task in store.list_tasks()], [created])

    def wait_for_brief(self, client, store):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = store.get_schedule()
            if state["latest_brief"]:
                return state
            if state["latest_run"] and state["latest_run"]["status"] == "failed":
                task_id = state["latest_run"]["task_id"]
                self.fail(client.get(f"/tasks/{task_id}/history").text)
            time.sleep(0.025)
        self.fail(f"No completed brief: {state}")

    def test_responsibility_dashboard_reopens_a_dated_brief_after_restart(self):
        model = BriefModel()
        with TestClient(create_app(data_dir=self.data, model=model, clock=lambda: THURSDAY)) as client:
            created = client.post("/responsibility", data={
                "request": REQUEST, "weekday": "4", "local_time": "09:00",
            }, follow_redirects=False)
            self.assertEqual(created.status_code, 303, created.text)
            self.assertTrue(created.headers["location"].endswith("/responsibility"))
            store = Store(self.data)
            schedule_id = store.get_schedule()["id"]
            dashboard = client.get("/responsibility")
            self.assertEqual(dashboard.status_code, 200, dashboard.text)
            self.assertIn(f'data-next-run-at="{int(FRIDAY_PDT.timestamp())}"', dashboard.text)
            self.assertEqual(store.list_tasks(), [])

        with TestClient(create_app(data_dir=self.data, model=model, clock=lambda: FRIDAY_PDT)) as client:
            schedule = self.wait_for_brief(client, store)
            self.assertEqual(schedule["id"], schedule_id)
            self.assertEqual(schedule["latest_run"]["status"], "completed")
            brief = schedule["latest_brief"]
            self.assertEqual(brief["due_at"], int(FRIDAY_PDT.timestamp()))
            self.assertEqual(brief["name"], "meeting-brief-2026-10-30.md")
            artifact = f"/tasks/{brief['task_id']}/artifacts/{brief['name']}"
            dashboard = client.get("/responsibility")
            self.assertIn("Open latest brief", dashboard.text)
            self.assertIn("Open latest assignment", dashboard.text)
            self.assertIn(artifact, dashboard.text)
            opened = client.get(artifact)
            self.assertEqual(opened.status_code, 200, opened.text)
            self.assertIn("Meeting brief — 2026-10-30", opened.text)
            download = client.get(artifact, params={"download": 1})
            self.assertEqual(download.text, "# Meeting brief — 2026-10-30\n\nReview official AI resources.\n")
            request, = model.requests
            self.assertEqual(request["uploaded_files"], [])
            self.assertTrue(request["request"].startswith(REQUEST))
            self.assertIn("Friday, October 30, 2026 at 9:00 AM PDT", request["request"])
            self.assertIn("Use 2026-10-30 Pacific as the run date", request["request"])
            stopped = client.post(f"/responsibility/{schedule_id}/stop", follow_redirects=False)
            self.assertEqual(stopped.status_code, 303, stopped.text)

        with TestClient(create_app(data_dir=self.data, model=model, clock=lambda: FRIDAY_PST)) as client:
            self.assertIn(artifact, client.get("/responsibility").text)
            self.assertEqual(client.get(artifact, params={"download": 1}).text, download.text)
            self.assertIsNone(store.enqueue_due_schedule(now=FRIDAY_PST))
            self.assertEqual([task["id"] for task in store.list_tasks()], [brief["task_id"]])
