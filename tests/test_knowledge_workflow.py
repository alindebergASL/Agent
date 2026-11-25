"""Revise a purchasing decision from immutable evidence in fresh browser assignments."""

import json
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from urllib.parse import urljoin

from fastapi.testclient import TestClient

from tests.quote_fixture import QUOTE_UPLOADS, Quote, answer, call, tool_outputs
from tests.test_connected_workflow import Links
from work_agent.web import create_app

SCOPE = "Atlas purchasing"
BASELINE = "Recommend a supplier for ten units in two shipments and one shipment. Maintain the Atlas purchasing note."
DECOY = "Read the archived terms for review and record a separate housekeeping report."
REVISE = ("The attached quote replaces Supplier A's terms. Find recommendations based on the old terms, "
          "reassess both shipment choices, and update the Atlas purchasing note.")
REPORT = "recommendation.md"


def packet(name, keyword):
    header, row = QUOTE_UPLOADS[name].splitlines()
    return header + b",catalog_mark\r\n" + row + b"," + keyword + b"\r\n"


ORIGINALS = {"packet-17.csv": packet("supplier-a.csv", b"meridian"),
             "packet-29.csv": packet("supplier-b.csv", b"juniper")}
UPDATED = {"packet-33.csv": ORIGINALS["packet-17.csv"].replace(b"30.00", b"20.00")}


def row_url(receipt):
    return receipt.get("revision_url", "../uploads/" + receipt["file_id"]) + "#L2"


def calculate(receipt, shipments, side):
    quote = Quote.read(receipt)
    url = row_url(receipt)
    return call(f"total_{shipments}_{side}", "calculate_with_evidence",
                expression=f"10*price+{quote.multiplier(shipments)}*freight", conditions=[],
                inputs=[{"name": "price", "value": str(quote.unit_price), "source_url": url,
                         "interpretation": "Quoted unit price in USD"},
                        {"name": "freight", "value": str(quote.freight), "source_url": url,
                         "interpretation": "Freight in USD " + quote.freight_basis}])


def publish(out, receipts, changed):
    quotes = [Quote.read(item) for item in receipts]
    sources = [row_url(item) for item in receipts]
    claims = []
    for shipments in (2, 1):
        totals = [Decimal(out[f"total_{shipments}_{side}"]["result"]) for side in ("a", "b")]
        winner = quotes[totals.index(min(totals))].supplier
        text = (f"{shipments} shipment(s): {winner} wins by ${abs(totals[0] - totals[1]):.2f}. "
                f"Supplier A ${totals[0]:.2f}, Supplier B ${totals[1]:.2f}.")
        claims.append({"text": text, "sources": sources})
    for quote, url in zip(quotes, sources):
        claims.append({"text": f"{quote.supplier} charges ${quote.freight:.2f} {quote.freight_basis}.",
                       "sources": [url]})
    if changed:
        original = out["report"]
        line = next(line for line in original["lines"] if "2 shipment(s)" in line["text"])
        previous = line["text"].split(" [")[0]
        old_terms = Quote.read(out["old_a"])
        change = f"Previously {previous} Revised freight replaces ${old_terms.freight:.2f} with ${quotes[0].freight:.2f}."
        claims.append({"text": change,
                       "sources": [f"{original['revision_url']}#L{line['number']}", row_url(out["old_a"]), sources[0]]})
    for number, claim in enumerate(claims, 1):
        claim["supersedes"] = number if changed and number <= 3 else None
    report = "\n\n".join(claim["text"] + " " + " ".join(
        f"[evidence {i}]({url})" for i, url in enumerate(claim["sources"], 1)
    ) for claim in claims)
    items = [call("save", "write_artifact", name=REPORT, content=report),
             call("save_note", "stage_project_note", scope=SCOPE, expected_revision=out["note"]["revision"],
                  claims=claims, reason=change if changed else "Original comparison from the two uploaded quotes.")]
    if changed:
        items.append(call("amendment", "stage_evidence_change", earlier_source=row_url(out["old_a"]),
                          new_source=sources[0], relationship="supersedes", scope=SCOPE, reason=change))
    return items


class KnowledgeQuoteModel:
    """Select sources using actual discovery receipts and calculate from the observed CSV rows."""

    def respond(self, history, tools, instructions, *, max_tool_calls):
        task = json.loads(next(item["content"] for item in reversed(history) if item.get("role") == "user"))
        request = task["request"]
        out = tool_outputs(history)
        failures = {key: result["error"] for key, result in out.items() if "error" in result}
        if failures:
            raise ValueError(str(failures))
        if "save" in out:
            items = [answer("The report is saved.")]
        elif request == DECOY and "old_a" in out:
            items = [call("save", "write_artifact", name="housekeeping.md", content="The catalog review is complete.")]
        elif request != BASELINE and "find_a" not in out:
            items = [call("find_a", "search_saved_work", query="meridian", kind="upload", paraphrases=[])]
            if request == REVISE:
                items.append(call("find_b", "search_saved_work", query="juniper", kind="upload", paraphrases=[]))
        elif request != BASELINE and "open_a" not in out:
            sides = ("a", "b") if request == REVISE else ("a",)
            items = [call(f"open_{side}", "open_saved_work",
                          candidate_id=out[f"find_{side}"]["candidates"][0]["candidate_id"]) for side in sides]
        elif request != BASELINE and "old_a" not in out:
            sides = ("a", "b") if request == REVISE else ("a",)
            items = [call(f"old_{side}", "read_prior_artifact", reference_id=out[f"open_{side}"]["reference_id"],
                          start_line=1, line_count=10) for side in sides]
            if request == REVISE:
                items.append(call("uses", "find_evidence_uses", source=out["open_a"]["reference_id"], offset=0))
        elif request == REVISE and "open_report" not in out:
            items = [call("open_report", "open_saved_work",
                          candidate_id=out["uses"]["citing_revisions"][0]["candidate_id"])]
        elif "note" not in out:
            items = [call("note", "read_project_note", scope=SCOPE)]
            for index, name in enumerate(task["uploaded_files"]):
                items.append(call(f"upload_{index}", "read_file", file_id=name, start_line=1, line_count=10))
            if request == REVISE:
                items.append(call("report", "read_prior_artifact", reference_id=out["open_report"]["reference_id"],
                                  start_line=1, line_count=30))
        else:
            receipts = [out["upload_0"], out["upload_1"] if request == BASELINE else out["old_b"]]
            if "total_2_a" not in out:
                items = [calculate(item, shipments, side) for shipments in (2, 1)
                         for side, item in zip(("a", "b"), receipts)]
            else:
                items = publish(out, receipts, request == REVISE)
        return {"status": "completed", "output": items}


class ReleasePolicyModel:
    """Use the returned review route for a design release, without any arithmetic."""

    def respond(self, history, tools, instructions, *, max_tool_calls):
        scope = "Design release"
        out = tool_outputs(history)
        if "save_note" in out:
            assert out["save_note"]["staged"], out["save_note"]
            items = [answer(out["rule"]["lines"][1]["text"])]
        elif "rule" in out:
            old = out["note"]
            row = out["rule"]["lines"][1]
            route = row["text"].split(",")[1]
            claim = {"text": "Route this design release to " + route + ".",
                     "sources": [row_url(out["rule"])], "supersedes": 1 if old["revision"] else None}
            reason = ("The revised release policy supersedes the earlier routing instruction. " +
                      old["claims"][0]["text"] if old["revision"] else "Original policy for design releases.")
            items = [call("save_note", "stage_project_note", scope=scope, expected_revision=old["revision"],
                          claims=[claim], reason=reason)]
        else:
            items = [call("note", "read_project_note", scope=scope),
                     call("rule", "read_file", file_id="policy.csv", start_line=1, line_count=2)]
        return {"status": "completed", "output": items}


class KnowledgeWorkflowTests(unittest.TestCase):
    def run_assignment(self, client, request, files=None):
        response = client.post("/tasks", data={"request": request}, follow_redirects=False,
                               files=[("files", (n, b, "text/csv")) for n, b in (files or {}).items()])
        self.assertEqual(response.status_code, 303)
        url = response.headers["location"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            history = client.get(url + "/history").json()
            if history["status"] == "completed":
                return url, tool_outputs(history["steps"])
            self.assertIn(history["status"], ("queued", "running"), history)
            time.sleep(.025)
        self.fail(f"Timed out waiting for {url}")

    def test_revise_only_citing_recommendations_preserving_original_evidence(self):
        data = Path(self.enterContext(tempfile.TemporaryDirectory()))

        with TestClient(create_app(data, KnowledgeQuoteModel())) as client:
            baseline, first = self.run_assignment(client, BASELINE, ORIGINALS)
            original_url = baseline + "/artifacts/" + REPORT
            original = client.get(original_url, params={"revision": 1, "download": 1}).content
            self.assertIn(b"2 shipment(s): Supplier B wins by $10.00. Supplier A $1060.00, Supplier B $1050.00.", original)
            self.assertIn(b"1 shipment(s): Supplier A wins by $20.00. Supplier A $1030.00, Supplier B $1050.00.", original)
            decoy_url, decoy = self.run_assignment(client, DECOY)
            self.assertEqual(decoy["old_a"]["lines"][1]["text"], ORIGINALS["packet-17.csv"].decode().splitlines()[1])
            updated, out = self.run_assignment(client, REVISE, UPDATED)
            for side, keyword in (("a", "meridian"), ("b", "juniper")):
                passage = out["find_" + side]["candidates"][0]["passage"]
                self.assertEqual((passage["start_line"], passage["end_line"]), (2, 2))
                self.assertIn(keyword, passage["text"])
            for history, side, receipt, basis in (
                (first, "a", first["upload_0"], "per shipment"),
                (out, "a", out["upload_0"], "per shipment"),
                (out, "b", out["old_b"], "per order"),
            ):
                bound = history["total_2_" + side]["inputs"][1]
                self.assertEqual(bound["source_text"], receipt["lines"][1]["text"])
                self.assertEqual(bound["source_url"], row_url(receipt))
                self.assertIn(basis, bound["interpretation"])
            hits = out["uses"]["citing_revisions"]
            self.assertEqual([(h["source_task_id"], h["name"], h["revision"], h["lines"]) for h in hits],
                             [(baseline.rsplit("/", 1)[-1], REPORT, 1, [2])])
            self.assertIn({"task_id": decoy_url.rsplit("/", 1)[-1], "read": True, "pinned": True},
                          out["uses"]["related_assignments"])
            self.assertEqual((first["note"]["revision"], out["note"]["revision"]), (0, 1))
            self.assertIn("Supplier B wins by $10.00", out["note"]["claims"][0]["text"])
            report_url = updated + "/artifacts/" + REPORT
            report = client.get(report_url, params={"download": 1}).content
            self.assertIn(b"2 shipment(s): Supplier A wins by $10.00. Supplier A $1040.00, Supplier B $1050.00.", report)
            self.assertIn(b"1 shipment(s): Supplier A wins by $30.00. Supplier A $1020.00, Supplier B $1050.00.", report)
            self.assertIn(b"Revised freight replaces $30.00 with $20.00", report)

        with TestClient(create_app(data, KnowledgeQuoteModel())) as client:
            self.assertEqual(client.get(original_url, params={"revision": 1, "download": 1}).content, original)
            self.assertEqual(client.get(report_url, params={"download": 1}).content, report)
            links = Links(client.get(report_url).text).hrefs
            for receipt, expected in ((out["old_a"], ORIGINALS["packet-17.csv"]),
                                      (out["old_b"], ORIGINALS["packet-29.csv"]), (out["upload_0"], UPDATED["packet-33.csv"])):
                link = row_url(receipt)
                self.assertIn(link, links)
                page = client.get(urljoin(report_url, link))
                self.assertIn('id="L2"', page.text)
                self.assertEqual(client.get(str(page.url.copy_add_param("download", "1"))).content, expected)
            self.assertIn("Previously", client.get("/notes/1?revision=2").text)
            self.assertIn("Supplier B wins by $10.00", client.get("/notes/1?revision=1").text)


    def test_qualitative_policy_change_keeps_the_earlier_rule_and_its_original_source(self):
        data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        prompt = "Who should receive the design release? Maintain the project note from the attached policy."
        with TestClient(create_app(data, ReleasePolicyModel())) as client:
            old, _ = self.run_assignment(client, prompt, {"policy.csv": b"release,route\ndesign,quality review\n"})
            new, out = self.run_assignment(client, prompt, {"policy.csv": b"release,route\ndesign,release manager\n"})
            self.assertEqual(out["note"]["revision"], 1)
        with TestClient(create_app(data, ReleasePolicyModel())) as client:
            for revision, task, route in ((1, old, "quality review"), (2, new, "release manager")):
                note = client.get("/notes/1", params={"revision": revision})
                self.assertEqual(note.status_code, 200)
                self.assertIn("Route this design release to " + route, note.text)
                link = task.removeprefix("http://testserver") + "/uploads/policy.csv#L2"
                self.assertIn(link, Links(note.text).hrefs)
                self.assertIn("design," + route, client.get(link).text)
