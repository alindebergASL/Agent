"""Reuse immutable uploads from another assignment through the normal app worker."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urljoin

from fastapi.testclient import TestClient

from tests.quote_fixture import QUOTE_UPLOADS, Quote, answer, call, tool_outputs
from tests.test_connected_workflow import Links
from work_agent.web import create_app


ARCHIVE = "Keep my supplier quotes and generate a sample supplier-a.csv with different prices."
COMPARE = "Find both original supplier quotes and compare twenty units in two shipments and one shipment."
ORIGINALS = {name: content.replace(b"\n", b"\r\n") for name, content in QUOTE_UPLOADS.items()}


class OriginalQuoteModel:
    def respond(self, history, tools, instructions, *, max_tool_calls):
        user = next(item for item in reversed(history) if item.get("role") == "user")
        request = json.loads(user["content"])["request"]
        out = tool_outputs(history)
        if "save" in out:
            items = [answer("Saved the file.")]
        elif request == ARCHIVE:
            items = [call("save", "write_artifact", name="supplier-a.csv",
                          content="supplier,unit_price_usd\nSupplier A,999.00\n")]
        elif "search" not in out:
            items = [call("search", "search_saved_work", query="supplier")]
        elif "open_0" not in out:
            originals = [hit for hit in out["search"]["candidates"] if hit["kind"] == "upload"]
            items = [call(f"open_{i}", "open_saved_work", candidate_id=hit["candidate_id"])
                     for i, hit in enumerate(originals)]
        elif "read_0" not in out:
            items = [call(f"read_{i}", "read_prior_artifact", reference_id=out[f"open_{i}"]["reference_id"],
                          start_line=1, line_count=2) for i in range(2)]
        else:
            receipts = [out[f"read_{i}"] for i in range(2)]
            quotes = [Quote.read(receipt) for receipt in receipts]
            report = []
            for shipments in (2, 1):
                totals = sorted((20 * q.unit_price + q.multiplier(shipments) * q.freight, q.supplier) for q in quotes)
                (low, winner), (high, other) = totals
                report.append(f"{shipments} shipment(s): {winner} ${low:.2f}, {other} ${high:.2f}; "
                              f"{winner} wins by ${high - low:.2f}.")
            report.extend(f"[{receipt['name']}, line 2]({receipt['revision_url']}#L2)" for receipt in receipts)
            items = [call("save", "write_artifact", name="comparison.md", content="\n\n".join(report))]
        return {"status": "completed", "output": items}


class OriginalUploadWorkflowTests(unittest.TestCase):
    def submit(self, client, request, originals=None):
        files = [("files", (name, data, "text/csv")) for name, data in (originals or {}).items()]
        response = client.post("/tasks", data={"request": request}, files=files, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        task = response.headers["location"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            history = client.get(task + "/history").json()
            if history["status"] == "completed":
                return task, history
            if history["status"] not in {"queued", "running"}:
                self.fail(str(history))
            time.sleep(.025)
        self.fail(f"Timed out waiting for {task}")

    def test_compare_originals_over_a_same_named_output_and_reopen_both_after_restart(self):
        data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with TestClient(create_app(data, OriginalQuoteModel())) as client:
            origin, _ = self.submit(client, ARCHIVE, ORIGINALS)
            task, history = self.submit(client, COMPARE)
            self.assertEqual(history["uploads"], [])
            out = tool_outputs(history["steps"])
            self.assertCountEqual([(hit["name"], hit["kind"]) for hit in out["search"]["candidates"]],
                                  [("supplier-a.csv", "artifact"), ("supplier-a.csv", "upload"), ("supplier-b.csv", "upload")])
            report_url = task + "/artifacts/comparison.md"
            report = client.get(report_url, params={"download": 1}).content
            self.assertIn(b"2 shipment(s): Supplier A $2060.00, Supplier B $2080.00; Supplier A wins by $20.00.", report)
            self.assertIn(b"1 shipment(s): Supplier A $2030.00, Supplier B $2080.00; Supplier A wins by $50.00.", report)

        with TestClient(create_app(data, OriginalQuoteModel())) as client:
            self.assertEqual(client.get(report_url, params={"download": 1}).content, report)
            rendered_links = Links(client.get(report_url).text).hrefs
            self.assertEqual(len(history["prior_artifacts"]), 2)
            for i in range(2):
                receipt = out[f"read_{i}"]
                name = receipt["name"]
                self.assertEqual(receipt["kind"], "upload")
                self.assertIsNone(receipt["revision"])
                self.assertEqual(receipt["source_task_id"], origin.rsplit("/", 1)[1])
                citation = receipt["revision_url"] + "#L2"
                self.assertIn(citation, rendered_links)
                original = client.get(urljoin(report_url, citation))
                self.assertEqual(str(original.url.copy_with(fragment=None)), origin + "/uploads/" + name)
                self.assertIn('id="L2"', original.text)
                self.assertIn(receipt["lines"][1]["text"], original.text)
                self.assertEqual(client.get(str(original.url.copy_add_param("download", "1"))).content, ORIGINALS[name])
