import csv
import json
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.quote_fixture import QUOTE_UPLOADS, REQUESTS, Quote, answer, call, tool_outputs
from work_agent import runner
from work_agent.storage import Store
from work_agent.web import create_app


BASELINE = "Save a purchase comparison template."
TWO_SHIPMENTS = next(request for request, shipments in REQUESTS.items() if shipments == 2)
CORRECTION = "We can take all ten units in one shipment. Update the comparison and recommendation."
MARKER = "Confirm that the worker is available."
NOTE = "Confirm receiving hours before placing the order."
BASE_CSV = "supplier,total_usd,source\n"
BASE_MD = "# Recommendation\n\nAwaiting comparison.\n"
EDITED_MD = BASE_MD + "\n" + NOTE + "\n"


def response(*output):
    return {"status": "completed", "output": list(output)}


def current_input(history):
    return json.loads(next(item["content"] for item in reversed(history) if item.get("role") == "user"))


class DelayedQuoteModel:
    def __init__(self):
        self.waiting = Event()
        self.release = Event()
        self.late_calls = ()

    def respond(self, history, tools, instructions, *, max_tool_calls):
        task = current_input(history)
        request = task["request"]
        outputs = tool_outputs(history)
        if request == BASELINE:
            if outputs:
                return response(answer("Saved a template."))
            return response(
                call("baseline-csv", "write_artifact", name="comparison.csv", content=BASE_CSV),
                call("baseline-md", "write_artifact", name="recommendation.md", content=BASE_MD),
            )
        if request == MARKER:
            return response(answer("Worker available."))

        shipments = {TWO_SHIPMENTS: 2, CORRECTION: 1}[request]
        if "save-csv" in outputs:
            return response(answer("Saved the corrected comparison."))
        if "quote-0" not in outputs:
            reads = [
                call(f"quote-{index}", "read_file", file_id=name, start_line=1, line_count=2)
                for index, name in enumerate(task["uploaded_files"])
            ]
            return response(
                *reads,
                call("base-md", "read_saved_artifact", name="recommendation.md", start_line=1, line_count=10),
                call("base-csv", "read_saved_artifact", name="comparison.csv", start_line=1, line_count=10),
            )

        quotes = [Quote.read(outputs[f"quote-{index}"]) for index in range(2)]
        if "total-0" not in outputs:
            return response(*(
                call(
                    f"total-{index}", "calculate",
                    expression=f"10 * {quote.unit_price} + {quote.multiplier(shipments)} * {quote.freight}",
                )
                for index, quote in enumerate(quotes)
            ))
        if shipments == 2:
            if "draft" not in outputs:
                return response(call("draft", "write_artifact", name="pending.md", content="Unpublished draft.\n"))
            self.waiting.set()
            if not self.release.wait(timeout=10):
                raise RuntimeError("Test did not release the delayed two-shipment response.")

        totals = {
            quote.supplier: Decimal(outputs[f"total-{index}"]["result"])
            for index, quote in enumerate(quotes)
        }
        winner = min(totals, key=totals.get)
        margin = max(totals.values()) - min(totals.values())
        table = BASE_CSV + "".join(
            f"{quote.supplier},{totals[quote.supplier]:.2f},{quote.source}\n" for quote in quotes
        )
        note, = (row["text"] for row in outputs["base-md"]["lines"] if row["text"] == NOTE)
        report = f"# Recommendation\n\nChoose {winner} by ${margin:.0f}.\n\n{note}\n"
        if shipments == 2:
            self.late_calls = (
                call("late-read", "read_file", file_id=quotes[0].source.split(":")[0], start_line=1, line_count=2),
                call("late-csv", "write_artifact", name="comparison.csv", content=table),
                call("late-md", "write_artifact", name="recommendation.md", content=report),
            )
            return response(*self.late_calls)
        return response(
            call("save-csv", "write_artifact", name="comparison.csv", content=table),
            call("save-md", "write_artifact", name="recommendation.md", content=report),
        )


class ResumeModel:
    def __init__(self):
        self.starting_contexts = []

    def respond(self, history, tools, instructions, *, max_tool_calls):
        outputs = tool_outputs(history)
        if "save" in outputs:
            return response(answer("Saved the inventory."))
        if "read" in outputs:
            source = outputs["read"]["lines"][-1]["text"]
            return response(call("save", "write_artifact", name="inventory.md", content=f"# Inventory\n\n{source}\n"))
        self.starting_contexts.append(list(history))
        file, = current_input(history)["uploaded_files"]
        return response(call("read", "read_file", file_id=file, start_line=1, line_count=2))


class TurnControlTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.data = Path(directory)

    def task_history(self, client, url):
        result = client.get(url + "/history")
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def wait_for_status(self, client, url, status):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            history = self.task_history(client, url)
            if history["status"] == status:
                return history
            time.sleep(0.025)
        self.fail(f"Assignment did not reach {status}: {history}")

    def submit(self, client, request, files=()):
        result = client.post("/tasks", data={"request": request}, files=files, follow_redirects=False)
        self.assertEqual(result.status_code, 303, result.text)
        return result.headers["location"]

    def control(self, client, url, action, turn_id, **fields):
        result = client.post(f"{url}/{action}", data={"turn_id": str(turn_id), **fields}, follow_redirects=False)
        self.assertEqual(result.status_code, 303, result.text)
        self.assertEqual(result.headers["location"], url)

    def delayed_comparison(self, client, model):
        files = [("files", (name, content, "text/csv")) for name, content in QUOTE_UPLOADS.items()]
        url = self.submit(client, BASELINE, files)
        self.wait_for_status(client, url, "completed")
        markdown = url + "/artifacts/recommendation.md"
        edit = client.post(markdown, data={"expected_revision": "1", "content": EDITED_MD}, follow_redirects=False)
        self.assertEqual(edit.status_code, 303, edit.text)
        follow_up = client.post(url + "/follow-up", data={"request": TWO_SHIPMENTS}, follow_redirects=False)
        self.assertEqual(follow_up.status_code, 303, follow_up.text)
        self.assertTrue(model.waiting.wait(5), self.task_history(client, url))
        turn = self.task_history(client, url)["turns"][-1]
        self.assertEqual(turn["status"], "running")
        return url, turn

    def assert_late_tools_blocked(self, model, dispatch, turn):
        late_read, late_csv, late_md = model.late_calls
        late_ids = {item["call_id"] for item in (late_read, late_csv, late_md)}
        self.assertIn("Supplier A,1060.00", json.loads(late_csv["arguments"])["content"])
        self.assertIn("Supplier B,1050.00", json.loads(late_csv["arguments"])["content"])
        self.assertIn("Choose Supplier B by $10.", json.loads(late_md["arguments"])["content"])
        self.assertTrue(late_ids.isdisjoint(item.args[0]["call_id"] for item in dispatch.call_args_list))
        self.assertTrue(late_ids.isdisjoint(tool_outputs(turn["steps"])))
        self.assertIsNone(turn["response"])

    def test_stop_blocks_late_tools_and_stops_a_queued_assignment(self):
        model = DelayedQuoteModel()
        with patch.object(runner, "_dispatch", wraps=runner._dispatch) as dispatch:
            with TestClient(create_app(data_dir=self.data, model=model)) as client:
                try:
                    url, old = self.delayed_comparison(client, model)
                    queued_url = self.submit(client, "This queued assignment should never run.")
                    queued = self.task_history(client, queued_url)["turns"][-1]
                    self.assertEqual(queued["status"], "queued")
                    self.control(client, queued_url, "stop", queued["id"])
                    self.control(client, url, "stop", old["id"])
                    self.assertEqual(self.task_history(client, url)["status"], "stopped")
                    model.release.set()
                    marker_url = self.submit(client, MARKER)
                    self.wait_for_status(client, marker_url, "completed")

                    stopped = self.task_history(client, url)["turns"][-1]
                    self.assertEqual(stopped["status"], "stopped")
                    self.assertEqual(stopped["steps"], old["steps"])
                    self.assert_late_tools_blocked(model, dispatch, stopped)
                    queued_stopped = self.task_history(client, queued_url)["turns"][-1]
                    self.assertEqual(queued_stopped["status"], "stopped")
                    self.assertEqual(queued_stopped["steps"], [])
                    self.assertEqual(client.get(url + "/artifacts/recommendation.md?download=1").text, EDITED_MD)
                    self.assertEqual(client.get(url + "/artifacts/comparison.csv?download=1").text, BASE_CSV)
                    self.assertEqual(client.get(url + "/artifacts/pending.md").status_code, 404)
                finally:
                    model.release.set()

    def test_correction_supersedes_late_tools_and_publishes_only_new_results(self):
        model = DelayedQuoteModel()
        with patch.object(runner, "_dispatch", wraps=runner._dispatch) as dispatch:
            with TestClient(create_app(data_dir=self.data, model=model)) as client:
                try:
                    url, old = self.delayed_comparison(client, model)
                    self.control(client, url, "correct", old["id"], request=CORRECTION)
                    corrected = self.task_history(client, url)
                    self.assertEqual(corrected["turns"][-2]["status"], "superseded")
                    self.assertEqual(corrected["turns"][-1]["status"], "queued")
                    stale_stop = client.post(url + "/stop", data={"turn_id": str(old["id"])})
                    self.assertEqual(stale_stop.status_code, 409, stale_stop.text)
                    self.assertEqual(client.get(url + "/artifacts/recommendation.md?download=1").text, EDITED_MD)
                    model.release.set()

                    final = self.wait_for_status(client, url, "completed")
                    superseded, current = final["turns"][-2:]
                    self.assertEqual(superseded["id"], old["id"])
                    self.assertEqual(superseded["status"], "superseded")
                    self.assertEqual(superseded["steps"], old["steps"])
                    self.assert_late_tools_blocked(model, dispatch, superseded)
                    self.assertEqual(current["request"], CORRECTION)
                    self.assertEqual(current["kind"], "correction")
                    self.assertEqual(current["status"], "completed")
                    self.assertEqual(tool_outputs(current["steps"])["base-md"]["revision"], 2)

                    csv_url = url + "/artifacts/comparison.csv"
                    markdown_url = url + "/artifacts/recommendation.md"
                    rows = list(csv.DictReader(client.get(csv_url, params={"download": 1}).text.splitlines()))
                    self.assertEqual(rows, [
                        {"supplier": "Supplier A", "total_usd": "1030.00", "source": "supplier-a.csv:2"},
                        {"supplier": "Supplier B", "total_usd": "1050.00", "source": "supplier-b.csv:2"},
                    ])
                    self.assertEqual(client.get(markdown_url, params={"download": 1}).text,
                                     f"# Recommendation\n\nChoose Supplier A by $20.\n\n{NOTE}\n")
                    self.assertEqual(client.get(markdown_url, params={"download": 1, "revision": 2}).text, EDITED_MD)
                    self.assertEqual(client.get(markdown_url, params={"revision": 4}).status_code, 404)
                    self.assertEqual(client.get(csv_url, params={"revision": 3}).status_code, 404)
                    self.assertEqual(client.get(url + "/artifacts/pending.md").status_code, 404)
                finally:
                    model.release.set()

    def test_continue_interrupted_first_turn_keeps_history_and_uploaded_bytes(self):
        content = b"part,units\r\n00427,12\r\n"
        store = Store(self.data)
        store.initialize()
        task_id = store.submit("Save an inventory report.", {"inventory.csv": content})
        self.assertEqual(store.claim(), task_id)
        old = store.get_run(task_id)["turn_id"]
        receipt = {"type": "function_call_output", "call_id": "read-before-restart", "output": "source read"}
        store.record_step(task_id, receipt, turn_id=old)
        store.stage(task_id, "pending.md", "Unpublished draft.\n", turn_id=old)

        with TestClient(create_app(data_dir=self.data, model=ResumeModel())) as client:
            task_url = f"http://testserver/tasks/{task_id}"
            interrupted = self.task_history(client, task_url)
            self.assertEqual(interrupted["status"], "interrupted")
            self.assertEqual(interrupted["turns"][-1]["steps"], [receipt])
            self.control(client, task_url, "continue", old)
            completed = self.wait_for_status(client, task_url, "completed")
            self.assertEqual([turn["status"] for turn in completed["turns"]], ["interrupted", "completed"])
            self.assertEqual(completed["turns"][0]["steps"], [receipt])
            self.assertEqual(completed["turns"][1]["kind"], "continuation")
            self.assertEqual(completed["turns"][1]["request"], completed["turns"][0]["request"])
            self.assertEqual(client.get(task_url + "/uploads/inventory.csv?download=1").content, content)
            self.assertEqual(client.get(task_url + "/artifacts/inventory.md?download=1").text, "# Inventory\n\n00427,12\n")
            self.assertEqual(client.get(task_url + "/artifacts/pending.md").status_code, 404)

    def test_new_request_after_stop_uses_uploads_and_keeps_completed_context(self):
        store = Store(self.data)
        store.initialize()
        uploads = {"inventory.csv": b"part,units\r\n00427,12\r\n"}
        first = store.submit("Discard these rows.", uploads)
        store.stop_turn(first, store.get_task(first)["turns"][0]["id"])

        later = store.submit("Keep identifiers padded.", uploads)
        self.assertEqual(store.claim(), later)
        store.stage(later, "baseline.md", "Identifier: 00427\n")
        store.complete(later, "Saved the baseline.")
        previous = store.follow_up(later, "Draft a removal notice.")
        self.assertEqual(store.claim(), later)
        corrected = store.correct_turn(later, previous, "Draft two removal notices.")
        store.stop_turn(later, corrected)

        model = ResumeModel()
        fresh = "Save an inventory report."
        cases = [
            (first, [], ["Discard these rows."]),
            (later, ["Keep identifiers padded.", "Saved the baseline."],
             ["Keep identifiers padded.", "Draft a removal notice.", "Draft two removal notices."]),
        ]
        with TestClient(create_app(data_dir=self.data, model=model)) as client:
            for task_id, expected_context, old_requests in cases:
                with self.subTest(task_id=task_id):
                    url = f"http://testserver/tasks/{task_id}"
                    page = client.get(url)
                    self.assertIn(f'action="{url}/follow-up"', page.text)
                    self.assertNotIn(f'action="{url}/continue"', page.text)
                    queued = client.post(url + "/follow-up", data={"request": fresh}, follow_redirects=False)
                    self.assertEqual(queued.status_code, 303, queued.text)
                    history = self.wait_for_status(client, url, "completed")
                    self.assertEqual([turn["request"] for turn in history["turns"]], [*old_requests, fresh])
                    self.assertEqual(history["turns"][-2]["status"], "stopped")
                    self.assertEqual(history["turns"][-1]["kind"], "follow-up")
                    context = model.starting_contexts[-1]
                    self.assertEqual([item["content"] for item in context[:-1]], expected_context)
                    self.assertEqual(current_input(context)["request"], fresh)
                    result = client.get(url + "/artifacts/inventory.md?download=1")
                    self.assertEqual(result.text, "# Inventory\n\n00427,12\n")
            saved = client.get(f"/tasks/{later}/artifacts/baseline.md?download=1")
            self.assertEqual(saved.text, "Identifier: 00427\n")
