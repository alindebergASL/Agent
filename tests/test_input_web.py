import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.web import create_app


REQUEST = "Save the delivered cost for ten units in two shipments. Use the quoted freight terms."
QUESTION = "How much does the supplier charge for freight per shipment?"
QUOTE = b"unit_price_usd\r\n100.00\r\n"
FREIGHT = b"freight_per_shipment_usd\r\n30.00\r\n"


class FreightModel:
    """Ask for missing evidence and use both source files when they become available."""

    def respond(self, history, tools, instructions, *, max_tool_calls):
        task = json.loads(next(item["content"] for item in reversed(history) if item.get("role") == "user"))
        outputs = tool_outputs(history)
        if "price" not in outputs:
            reads = [call("price", "read_file", file_id="quote.csv", start_line=2, line_count=1)]
            if "freight.csv" in task.get("attached_files", []):
                reads.append(call("rate", "read_file", file_id="freight.csv", start_line=2, line_count=1))
            items = reads
        elif "rate" not in outputs:
            items = [call("ask", "request_user_input", question=QUESTION)]
        elif "cost" not in outputs:
            price = outputs["price"]["lines"][0]["text"]
            rate = outputs["rate"]["lines"][0]["text"]
            items = [call("cost", "calculate", expression=f"10 * {price} + 2 * {rate}")]
        elif "save" not in outputs:
            total = outputs["cost"]["result"]
            items = [call("save", "write_artifact", name="cost.md", content=f"Delivered cost: ${total}.\n")]
        else:
            items = [answer("Saved the delivered cost.")]
        return {"status": "completed", "output": items}


class ClarificationWebTests(unittest.TestCase):
    def test_restart_and_answer_with_new_source(self):
        data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with TestClient(create_app(data, FreightModel())) as client:
            submitted = client.post("/tasks", data={"request": REQUEST}, files={"files": ("quote.csv", QUOTE)},
                                    follow_redirects=False)
            self.assertEqual(submitted.status_code, 303, submitted.text)
            url = submitted.headers["location"]
            waiting = self.wait_for(client, url, "waiting")
            turn = waiting["turns"][0]
            self.assertEqual(turn["response"], QUESTION)
            self.assertEqual(tool_outputs(turn["steps"])["price"]["lines"][0]["text"], "100.00")
            self.assertEqual(client.get(url + "/artifacts/cost.md").status_code, 404)

        with TestClient(create_app(data, FreightModel())) as client:
            self.assertEqual(client.get(url + "/history").json(), waiting)
            page = client.get(url).text
            self.assertIn(QUESTION, page)
            self.assertIn(f'action="{url}/answer"', page)
            fields = {"turn_id": str(turn["id"])}
            invalid = client.post(url + "/answer", data=fields, files={"files": ("freight.csv", b"\xff")})
            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(client.get(url + "/history").json(), waiting)
            resumed = client.post(url + "/answer", data=fields, files={"files": ("freight.csv", FREIGHT)},
                                  follow_redirects=False)
            self.assertEqual(resumed.status_code, 303, resumed.text)
            final = self.wait_for(client, url, "completed")
            self.assertEqual([(t["kind"], t["status"]) for t in final["turns"]],
                             [("initial", "waiting"), ("answer", "completed")])
            self.assertEqual(final["turns"][0], turn)
            self.assertEqual(final["turns"][1]["attached_files"], ["freight.csv"])
            self.assertEqual(client.get(url + "/artifacts/cost.md?download=1").content,
                             b"Delivered cost: $1060.00.\n")
            self.assertEqual(client.get(url + "/uploads/quote.csv?download=1").content, QUOTE)
            self.assertEqual(client.get(url + "/uploads/freight.csv?download=1").content, FREIGHT)
            stale = client.post(url + "/answer", data={**fields, "request": "A second browser tab."})
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(client.get(url + "/history").json(), final)

    def wait_for(self, client, url, status):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            history = client.get(url + "/history").json()
            if history["status"] == status:
                return history
            if history["status"] == "failed":
                self.fail(history["error"])
            time.sleep(0.025)
        self.fail(f"Expected {status}, found {history['status']}")
