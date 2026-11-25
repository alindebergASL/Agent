"""Exercise real MCP HTTP reads and the app UI with a simulated Responses program."""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urljoin, urlsplit

import uvicorn
from fastapi.testclient import TestClient

from tests.connected_source_fixture import create_server
from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import AssignmentError
from work_agent.web import create_app


@contextmanager
def serve_source(role, path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        app = create_server(role, path).streamable_http_app(stateless_http=True, json_response=True)
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError(f"{role} fixture could not start")
                time.sleep(0.02)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            server.should_exit = True
            thread.join(timeout=5)


CALLER = {"type": "program", "caller_id": "source-batch"}
READS = {"purchasing": "list_orders", "warehouse": "list_receipts"}


class ReconciliationModel:
    """Simulate provider callbacks and reduction; the app dispatches the actual reads."""

    def respond(self, history, tools, instructions, *, max_tool_calls):
        advertised = {tool.get("name") for tool in tools if "programmatic" in tool.get("allowed_callers", [])}
        required = {f"mcp_{source}__{name}" for source, name in READS.items()}
        if not required.issubset(advertised):
            raise AssignmentError(f"Missing program tools: {sorted(required - advertised)}")
        outputs = tool_outputs(history)
        for source in READS:
            if "error" in outputs.get(source, {}):
                raise AssignmentError(f"{source} failed: {outputs[source]['error']}")
        reduction = next((item["result"] for item in history if item["type"] == "program_output"), None)
        if "save" in outputs:
            items = [answer("Saved the shortage report.")]
        elif reduction:
            items = [call("save", "write_artifact", name="shortages.md", content=reduction)]
        elif "purchasing" in outputs:
            order, = outputs["purchasing"]["data"]["records"]
            receipts = outputs["warehouse"]["data"]["records"]
            received = sum(
                int(row["received_qty"]) for row in receipts
                if (row["po_number"], row["po_line"]) == (order["po_number"], order["po_line"])
            )
            short = int(order["ordered_qty"]) - received
            report = (
                f"PO {order['po_number']} is short {short} units.\n\n"
                f"[Order]({outputs['purchasing']['evidence_url']}) · "
                f"[Receipts]({outputs['warehouse']['evidence_url']})\n"
            )
            items = [{"type": "program_output", "call_id": CALLER["caller_id"], "status": "completed",
                      "result": report}]
        else:
            items = [{"type": "program", "call_id": CALLER["caller_id"], "code":
                      "const [orders,receipts] = await Promise.all([tools.mcp_purchasing__list_orders({}), "
                      "tools.mcp_warehouse__list_receipts({})]);"}]
            for source, name in READS.items():
                items.append({**call(source, f"mcp_{source}__{name}", cursor=0), "caller": CALLER})
        return {"status": "completed", "output": items}


class Links(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.hrefs = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href")
        if tag == "a" and href:
            self.hrefs.append(href)


class ConnectedWorkflowTests(unittest.TestCase):
    def run_report(self, client, expected_short):
        response = client.post("/tasks", data={"request": "Compare purchasing with warehouse and save the shortage report."},
                               follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        assignment = response.headers["location"]
        deadline = time.monotonic() + 10
        while True:
            history = client.get(assignment + "/history").json()
            if history["status"] == "completed":
                break
            if history["status"] == "failed" or time.monotonic() >= deadline:
                self.fail(history.get("error") or history["status"])
            time.sleep(0.025)
        receipts = {
            step["call_id"]: step for step in history["steps"] if step["type"] == "function_call_output"
        }
        self.assertEqual(history["uploads"], [])
        artifact = assignment + "/artifacts/shortages.md"
        page = client.get(artifact)
        self.assertEqual(page.status_code, 200)
        self.assertIn(f"PO 000731 is short {expected_short} units.", page.text)
        rendered = {urljoin(artifact, href) for href in Links(page.text).hrefs}
        citations = {}
        for source, name in READS.items():
            self.assertEqual(receipts[source]["caller"], CALLER)
            output = json.loads(receipts[source]["output"])
            self.assertEqual((output["source"], output["tool"]), (source, name))
            citation = urljoin(artifact, output["evidence_url"])
            self.assertIn(citation, rendered)
            evidence = client.get(citation)
            self.assertEqual(evidence.status_code, 200)
            self.assertIn(output["data"]["records"][0]["source_ref"], evidence.text)
            citations[source] = citation
        return assignment, citations

    def test_two_connected_sources_keep_the_evidence_used_by_each_assignment(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        orders = folder / "purchase-orders.csv"
        orders.write_text("po_number,po_line,ordered_qty\n000731,1,15\n")
        receipts = folder / "receipts.csv"
        initial = "receipt_id,po_number,po_line,received_qty\nRC-01,000731,1,5\nRC-02,000731,1,2\nRC-03,731,1,20\n"
        receipts.write_text(initial)
        config = folder / "sources.json"
        data = folder / "state"
        with serve_source("purchasing", orders) as purchasing, serve_source("warehouse", receipts) as warehouse:
            config.write_text(json.dumps({"sources": [
                {"name": "purchasing", "url": purchasing, "tools": ["list_orders"]},
                {"name": "warehouse", "url": warehouse, "tools": ["list_receipts"]},
            ]}))
            with patch.dict(os.environ, {"WORK_AGENT_MCP_CONFIG": str(config)}):
                with TestClient(create_app(data, ReconciliationModel())) as client:
                    original, first_links = self.run_report(client, 8)
                    first = client.get(first_links["warehouse"]).content
                    self.assertIn(b"RC-01", first)
                    receipts.write_text(initial.replace("RC-01,000731,1,5", "RC-04,000731,1,9"))
                    updated, new_links = self.run_report(client, 4)
                    self.assertNotEqual(first_links["warehouse"], new_links["warehouse"])
                    self.assertIn(b"RC-04", client.get(new_links["warehouse"]).content)
                    old_suffix = urlsplit(first_links["warehouse"]).path.rsplit("/", 1)[1]
                    self.assertEqual(client.get(updated + "/evidence/" + old_suffix).status_code, 404)

        with TestClient(create_app(data, ReconciliationModel())) as client:
            self.assertEqual(client.get(first_links["warehouse"]).content, first)
            self.assertIn("short 8 units", client.get(original + "/artifacts/shortages.md").text)
