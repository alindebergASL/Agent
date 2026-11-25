"""Exercise discovery in fresh assignments with scripted Responses and a local campus source."""

import csv
import json
import os
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urljoin

from fastapi.testclient import TestClient
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from tests.quote_fixture import answer, call, tool_outputs
from tests.test_connected_workflow import Links, serve_mcp
from work_agent.web import create_app


IOWA = "Prepare a meeting brief for the University of Iowa."
STATE = "Prepare a meeting brief for Iowa State University."
FRUIT = "Save the fruit purchases in a CSV."
SEEDS = {
    IOWA: ("brief.md", "The University of Iowa sandbox pilot has no announced start date."),
    STATE: ("brief.md", "Iowa State University expanded its library hours."),
    FRUIT: ("purchases.csv", "Item,Price,Quantity Sold\nBananas,1,10\nApples,1.50,20\nKiwis,2,5\n"),
}
REFRESH = "Update my University of Iowa meeting prep with what changed and three questions worth asking."
SCENARIO = "Find the purchases CSV from last time. What if apples sold for $2? Cite the original rows."


def fresh_source():
    source = MCPServer("campus")

    @source.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def read_update() -> dict[str, str]:
        """Read the latest campus pilot bulletin (synthetic test data)."""
        return {"pilot": "sandbox pilot", "start": "December 8", "owner": "Research IT"}

    return source


class SavedWorkModel:
    def respond(self, history, tools, instructions, *, max_tool_calls):
        user = next(item for item in reversed(history) if item.get("role") == "user")
        request = json.loads(user["content"])["request"]
        outputs = tool_outputs(history)
        if "save" in outputs:
            items = [answer("Saved the result.")]
        elif request in SEEDS:
            name, content = SEEDS[request]
            items = [call("save", "write_artifact", name=name, content=content)]
        elif "hits" not in outputs:
            query = "meeting brief" if request == REFRESH else "purchases.csv"
            items = [call("hits", "search_saved_work", query=query)]
        elif "opened" not in outputs:
            hits = outputs["hits"]["candidates"]
            if request == REFRESH:
                hit, = [row for row in hits if "University of Iowa" in row["request"]]
            else:
                hit, = [row for row in hits if row["name"] == "purchases.csv"]
            items = [call("opened", "open_saved_work", candidate_id=hit["candidate_id"])]
        elif "prior" not in outputs:
            reference = outputs["opened"]["reference_id"]
            items = [call("prior", "read_prior_artifact", reference_id=reference, start_line=1, line_count=100)]
            if request == REFRESH:
                items.append(call("update", "mcp_campus__read_update"))
        else:
            prior = outputs["prior"]
            lines = [line["text"] for line in prior["lines"]]
            if request == REFRESH:
                update = outputs["update"]
                bulletin = update["data"]
                content = (
                    f"Earlier brief: {lines[0]} [Prior revision]({prior['revision_url']})\n\n"
                    f"The {bulletin['pilot']} starts {bulletin['start']}. [Bulletin]({update['evidence_url']})\n\n"
                    f"1. Who is in the first cohort on {bulletin['start']}?\n"
                    f"2. How will {bulletin['owner']} measure success?\n"
                    f"3. What support is available for the {bulletin['pilot']}?"
                )
            else:
                rows = csv.DictReader(lines)
                total = sum((Decimal(2) if row["Item"] == "Apples" else Decimal(row["Price"]))
                            * int(row["Quantity Sold"]) for row in rows)
                content = f"With apples at $2, revenue is ${total:.2f}. [Original lines 2–4]({prior['revision_url']}#L2)"
            items = [call("save", "write_artifact", name="result.md", content=content)]
        return {"status": "completed", "output": items}


class SavedWorkWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = self.folder / "data"

    def submit(self, client, request):
        response = client.post("/tasks", data={"request": request}, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        task = response.headers["location"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            history = client.get(task + "/history").json()
            if history["status"] == "completed":
                self.assertEqual(history["uploads"], [])
                return task, tool_outputs(history["steps"])
            if history["status"] not in {"queued", "running"}:
                self.fail(history.get("error") or history["status"])
            time.sleep(0.025)
        self.fail(f"Timed out waiting for {task}")

    def test_find_right_university_compare_new_source_and_reopen_pinned_evidence(self):
        with TestClient(create_app(self.data, SavedWorkModel())) as client:
            source_task, _ = self.submit(client, IOWA)
            self.submit(client, STATE)  # Newer, same filename, different institution.

        with serve_mcp(fresh_source()) as endpoint:
            config = self.folder / "sources.json"
            config.write_text(json.dumps({"sources": [{"name": "campus", "url": endpoint, "tools": ["read_update"]}]}))
            with patch.dict(os.environ, {"WORK_AGENT_MCP_CONFIG": str(config)}):
                with TestClient(create_app(self.data, SavedWorkModel())) as client:
                    task, out = self.submit(client, REFRESH)
                    self.assertCountEqual([row["request"] for row in out["hits"]["candidates"]], [STATE, IOWA])
                    self.assertEqual(out["prior"]["source_task_id"], source_task.rsplit("/", 1)[1])
                    artifact = task + "/artifacts/result.md"
                    report = client.get(artifact)
                    self.assertIn("December 8", report.text)
                    self.assertIn(SEEDS[IOWA][1], report.text)
                    self.assertNotIn("library hours", report.text)
                    links = Links(report.text).hrefs
                    self.assertIn(out["update"]["evidence_url"], links)
                    self.assertIn(out["prior"]["revision_url"], links)
                    update = urljoin(artifact, out["update"]["evidence_url"])
                    previous = urljoin(artifact, out["prior"]["revision_url"])
                    content = client.get(artifact, params={"download": 1}).text
                    self.assertEqual(content.count("?"), 3)
                    source = source_task + "/artifacts/brief.md"
                    self.assertEqual(client.post(source, data={"expected_revision": "1", "content": "Updated later."},
                                                 follow_redirects=False).status_code, 303)

        with TestClient(create_app(self.data)) as client:
            prior = client.get(previous)
            self.assertEqual(str(prior.url), source + "?revision=1")
            self.assertIn(SEEDS[IOWA][1], prior.text)
            self.assertNotIn("Updated later.", prior.text)
            self.assertIn("Updated later.", client.get(source).text)
            self.assertIn("Research IT", client.get(update).text)
            self.assertEqual(client.get(artifact, params={"download": 1}).text, content)

    def test_find_csv_and_price_a_scenario_in_a_fresh_assignment(self):
        with TestClient(create_app(self.data, SavedWorkModel())) as client:
            origin, _ = self.submit(client, FRUIT)
            task, out = self.submit(client, SCENARIO)
            artifact = task + "/artifacts/result.md"
            report = client.get(artifact)
            self.assertIn("$60.00", report.text)
            self.assertIn("Original lines 2–4", report.text)
            self.assertEqual(out["prior"]["lines"][2], {"number": 3, "text": "Apples,1.50,20"})
            row_link = out["prior"]["revision_url"] + "#L2"
            self.assertIn(row_link, Links(report.text).hrefs)
            # Browsers carry the fragment through a redirect with no explicit fragment.
            csv_page = client.get(urljoin(artifact, row_link))
            self.assertIn('id="L2"', csv_page.text)
            self.assertIn("Bananas,1,10", csv_page.text)
            source = csv_page.url.copy_with(fragment=None)
            self.assertEqual(str(source), origin + "/artifacts/purchases.csv?revision=1")
            original = client.get(str(source.copy_add_param("download", "1")))
            self.assertEqual(original.content, SEEDS[FRUIT][1].encode())
