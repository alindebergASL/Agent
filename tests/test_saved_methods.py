import csv
import io
import json
import re
import tempfile
import time
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from tests.quote_fixture import QUOTE_UPLOADS, REQUESTS, QuoteModel, answer, call, tool_outputs
from work_agent.web import create_app


SOURCE_REQUEST = next(request for request, shipments in REQUESTS.items() if shipments == 2)
FRESH_REQUEST = "Compare the attached tariffs for ten units delivered in two shipments. Save both results."
SOURCE_NOTE = "Private receiving note: contact Morgan before 08:30."
EVIDENCE = "Private evidence: the old recommendation was Supplier B by $10 and $1,050 delivered."
METHOD = {
    "title": "Compare quoted freight with part identifiers",
    "scope": "Supplier quotes containing merchandise prices and per-shipment or per-order freight.",
    "procedure": "Preserve part identifiers as text. Keep source rows with the unit and freight terms.",
    "evidence": EVIDENCE,
}
EDITED_PROCEDURE = METHOD["procedure"] + " Compare merchandise and freight separately."
FRESH_QUOTES = {
    "harbor.csv": b"part;company;freight_basis;shipping;unit\n00708;Harbor Works;per shipment;12;88\n",
    "summit.csv": b"company;unit;part;shipping;freight_basis\nSummit Parts;90;00708;27;per order\n",
}


def complete(*output):
    return {"status": "completed", "output": list(output)}


def current_input(history):
    return json.loads(next(item["content"] for item in reversed(history) if item.get("role") == "user"))


class SourceQuoteModel(QuoteModel):
    def respond(self, history, tools, instructions, *, max_tool_calls):
        return super().respond(history, tools, instructions)


class RelatedQuoteModel:
    def __init__(self):
        self.requests = []

    def respond(self, history, tools, instructions, *, max_tool_calls):
        self.requests.append(deepcopy(history))
        task = current_input(history)
        receipts = tool_outputs(history)
        if "method" not in receipts:
            method, = task["saved_methods"]
            return complete(call("method", "read_method", method_id=method["id"]))
        if "quote-0" not in receipts:
            return complete(*(
                call(f"quote-{index}", "read_file", file_id=name, start_line=1, line_count=2)
                for index, name in enumerate(task["uploaded_files"])
            ))
        if "comparison" in receipts:
            return complete(answer("Saved the new quote comparison and recommendation."))

        source_lines = [receipts[f"quote-{index}"] for index in range(2)]
        quotes = []
        for source in source_lines:
            data = "\n".join(line["text"] for line in source["lines"])
            quote, = csv.DictReader(io.StringIO(data), delimiter=";")
            quotes.append({**quote, "source": f"{source['file_id']}:{source['lines'][-1]['number']}"})
        if "total-0" not in receipts:
            return complete(*(
                call(
                    f"total-{index}", "calculate",
                    expression=f"10 * {quote['unit']} + {quote['shipping']} * "
                               f"{2 if quote['freight_basis'] == 'per shipment' else 1}",
                )
                for index, quote in enumerate(quotes)
            ))

        totals = [Decimal(receipts[f"total-{index}"]["result"]) for index in range(2)]
        cheaper = quotes[totals.index(min(totals))]
        csv_text = "supplier,part,total_usd,source\n" + "".join(
            f"{quote['company']},{quote['part']},{total:.2f},{quote['source']}\n"
            for quote, total in zip(quotes, totals, strict=True)
        )
        recommendation = (
            f"Choose {cheaper['company']} for part `{cheaper['part']}`. "
            f"It costs ${max(totals) - min(totals):.0f} less.\n"
            f"Sources: {', '.join(quote['source'] for quote in quotes)}.\n"
        )
        return complete(
            call("comparison", "write_artifact", name="comparison.csv", content=csv_text),
            call("recommendation", "write_artifact", name="recommendation.md", content=recommendation),
        )


class RemovedMethodModel:
    def __init__(self, method_id):
        self.method_id = method_id
        self.first_request = None
        self.offered_tools = None

    def respond(self, history, tools, instructions, *, max_tool_calls):
        if "removed" in tool_outputs(history):
            return complete(answer("The saved method is unavailable."))
        self.first_request = current_input(history)
        self.offered_tools = tools
        return complete(call("removed", "read_method", method_id=self.method_id))


class SavedMethodTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.data = Path(directory)

    def submit(self, client, request, quotes=None):
        files = [("files", (name, content, "text/csv")) for name, content in (quotes or {}).items()]
        result = client.post("/tasks", data={"request": request}, files=files, follow_redirects=False)
        self.assertEqual(result.status_code, 303, result.text)
        return result.headers["location"]

    def completed_history(self, client, url):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = client.get(url + "/history")
            self.assertEqual(result.status_code, 200, result.text)
            history = result.json()
            if history["status"] == "completed":
                return history
            if history["status"] == "failed":
                self.fail(history["error"])
            time.sleep(0.025)
        self.fail(f"Assignment did not finish: {history}")

    def save_source_method(self, client):
        origin = self.submit(client, SOURCE_REQUEST, QUOTE_UPLOADS)
        old_history = self.completed_history(client, origin)
        old_markdown = origin + "/artifacts/recommendation.md"
        old_result = client.get(old_markdown, params={"download": 1}).text
        self.assertIn("Choose Supplier B", old_result)
        updated = client.post(
            old_markdown,
            data={"expected_revision": "1", "content": old_result + "\n" + SOURCE_NOTE + "\n"},
            follow_redirects=False,
        )
        self.assertEqual(updated.status_code, 303, updated.text)
        self.assertIn(SOURCE_NOTE, client.get(old_markdown, params={"download": 1}).text)
        saved = client.post(origin + "/save-method", data=METHOD, follow_redirects=False)
        self.assertEqual(saved.status_code, 303, saved.text)
        return origin, old_history["response"], saved.headers["location"]

    def revision(self, page):
        token = re.search(r'name="expected_revision" value="([0-9]+)"', page.text)
        self.assertIsNotNone(token, page.text)
        return token.group(1)

    def test_related_run_retrieves_only_scoped_method_after_restart(self):
        with TestClient(create_app(data_dir=self.data, model=SourceQuoteModel())) as client:
            origin, old_response, method_url = self.save_source_method(client)

        model = RelatedQuoteModel()
        with TestClient(create_app(data_dir=self.data, model=model)) as client:
            detail = client.get(method_url)
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertIn(EVIDENCE, detail.text)
            self.assertIn(origin, detail.text)
            edit_form = client.get(method_url, params={"edit": 1})
            edit = client.post(method_url, data={
                **METHOD, "procedure": EDITED_PROCEDURE, "expected_revision": self.revision(edit_form),
            }, follow_redirects=False)
            self.assertEqual(edit.status_code, 303, edit.text)
            self.assertEqual(edit.headers["location"], method_url)

            fresh = self.submit(client, FRESH_REQUEST, FRESH_QUOTES)
            self.assertNotEqual(fresh, origin)
            history = self.completed_history(client, fresh)
            self.assertEqual(history["uploads"], list(FRESH_QUOTES))
            self.assertEqual(len(history["turns"]), 1)
            steps = history["turns"][0]["steps"]

            method_metadata = {"id": int(method_url.rsplit("/", 1)[-1]), "title": METHOD["title"], "scope": METHOD["scope"]}
            first, = model.requests[0]
            self.assertEqual(json.loads(first["content"]), {
                "request": FRESH_REQUEST,
                "uploaded_files": list(FRESH_QUOTES),
                "saved_methods": [method_metadata],
            })
            self.assertEqual(tool_outputs(steps)["method"], {**method_metadata, "procedure": EDITED_PROCEDURE})
            asked = [
                output
                for step in steps if step["type"] == "response"
                for output in step["response"]["output"]
                if output["type"] == "function_call" and output["name"] == "read_method"
            ]
            self.assertEqual([json.loads(item["arguments"]) for item in asked], [{"method_id": method_metadata["id"]}])

            upstream = json.dumps(model.requests)
            self.assertIn(EDITED_PROCEDURE, upstream)
            for private in (
                SOURCE_REQUEST, old_response, SOURCE_NOTE, EVIDENCE, origin.rsplit("/", 1)[-1],
                "Supplier B", "1050", "1060", "supplier-a.csv", "supplier-b.csv",
            ):
                self.assertNotIn(private, upstream)

            saved_csv = client.get(fresh + "/artifacts/comparison.csv?download=1").text
            self.assertEqual(list(csv.DictReader(saved_csv.splitlines())), [
                {"supplier": "Harbor Works", "part": "00708", "total_usd": "904.00", "source": "harbor.csv:2"},
                {"supplier": "Summit Parts", "part": "00708", "total_usd": "927.00", "source": "summit.csv:2"},
            ])
            saved_md = client.get(fresh + "/artifacts/recommendation.md?download=1").text
            self.assertIn("Choose Harbor Works for part `00708`. It costs $23 less.", saved_md)

    def test_removed_method_is_not_offered_or_readable(self):
        with TestClient(create_app(data_dir=self.data, model=SourceQuoteModel())) as client:
            _, _, method_url = self.save_source_method(client)
            removed = client.post(method_url + "/remove", data={
                "expected_revision": self.revision(client.get(method_url)),
            }, follow_redirects=False)
            self.assertEqual(removed.status_code, 303, removed.text)
            self.assertEqual(client.get(method_url).status_code, 404)
            self.assertNotIn(METHOD["title"], client.get("/methods").text)

        model = RemovedMethodModel(int(method_url.rsplit("/", 1)[-1]))
        with TestClient(create_app(data_dir=self.data, model=model)) as client:
            fresh = self.submit(client, "Check whether a saved quote method is available.")
            history = self.completed_history(client, fresh)

        self.assertNotIn("saved_methods", model.first_request)
        self.assertNotIn("read_method", [tool.get("name") for tool in model.offered_tools])
        removed_receipt = tool_outputs(history["turns"][0]["steps"])["removed"]
        self.assertIn("error", removed_receipt)
        self.assertNotIn("procedure", removed_receipt)

    def test_stale_forms_preserve_current_method_and_submitted_draft(self):
        with TestClient(create_app(data_dir=self.data, model=SourceQuoteModel())) as client:
            _, _, url = self.save_source_method(client)
            original = self.revision(client.get(url, params={"edit": 1}))
            old_remove = self.revision(client.get(url))
            changed = {**METHOD, "scope": "Freight quotes with multiple currencies."}
            edited = client.post(url, data={**changed, "expected_revision": original}, follow_redirects=False)
            self.assertEqual(edited.status_code, 303)

            draft = {**METHOD, "procedure": "Check the currency before adding freight."}
            stale = client.post(url, data={**draft, "expected_revision": original})
            self.assertEqual(stale.status_code, 409)
            self.assertIn(draft["procedure"], stale.text)
            self.assertIn(changed["scope"], stale.text)
            current = client.get(url)
            self.assertNotIn(draft["procedure"], current.text)
            self.assertIn(changed["scope"], current.text)
            rejected = client.post(url + "/remove", data={"expected_revision": old_remove})
            self.assertEqual(rejected.status_code, 409)
            self.assertIn(changed["scope"], rejected.text)

            merged = {**draft, "scope": changed["scope"], "expected_revision": self.revision(stale)}
            saved = client.post(url, data=merged, follow_redirects=False)
            self.assertEqual(saved.status_code, 303)
            latest = client.get(url)
            self.assertIn(changed["scope"], latest.text)
            self.assertIn(draft["procedure"], latest.text)
            removed = client.post(url + "/remove", data={"expected_revision": self.revision(latest)}, follow_redirects=False)
            self.assertEqual(removed.status_code, 303)
            after_removal = client.post(url, data=merged)
            self.assertEqual(after_removal.status_code, 404)
            self.assertIn(draft["procedure"], after_removal.text)
            self.assertEqual(client.get(url).status_code, 404)
