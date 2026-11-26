"""Exercise native PDF evidence through the web form, worker, and durable citations."""

import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from fastapi.testclient import TestClient

from tests.pdf_example_files import AMENDMENT, HANDBOOK, MEASURE, POLICY, REVISE, SEED, TRIAL, example_files
from tests.quote_fixture import answer, call, tool_outputs
from tests.test_connected_workflow import Links
from work_agent.web import create_app


def source_line(receipt, prefix):
    return next(row for row in receipt["lines"] if row["text"].startswith(prefix))


def linked(row):
    return f"[Page {row['page']}]({row['evidence_url']})"


def decision_tools(outputs, changed):
    old = source_line(outputs["old"], "All canister transfers")
    label = source_line(outputs["old"], "All canisters must")
    deciding = [old]
    reason = "Published field handbook."
    if changed:
        deciding = [source_line(outputs["new"], "Sealed canister transfers"),
                    source_line(outputs["new"], "Open Ridge canisters")]
        reason = f"The approved amendment qualifies the original rule. Previously: {old['text']}"
    claims = [{"text": " ".join(row["text"] for row in deciding),
               "sources": [row["evidence_url"] for row in deciding], "supersedes": 1 if changed else None},
              {"text": label["text"], "sources": [label["evidence_url"]], "supersedes": None}]
    rows = [*deciding, label]
    report = "\n\n".join(f"{row['text']} {linked(row)}" for row in rows)
    if changed:
        report += f"\n\nPreviously: {old['text']} {linked(old)}"
    staged = [call("save", "write_artifact", name="decision.md", content=report),
              call("note_save", "stage_project_note", scope="Glacier field operations",
                   expected_revision=outputs["note"]["revision"], claims=claims, reason=reason)]
    if changed:
        staged.append(call("qualify", "stage_evidence_change", earlier_source=old["evidence_url"],
                           new_source=deciding[0]["evidence_url"], relationship="qualifies",
                           scope="Sealed transfers at Ridge", reason=reason))
    return staged


class PdfWorkflowModel:
    """Select and cite the lines actually returned by the application tools."""

    def respond(self, history, tools, instructions, *, max_tool_calls):
        out = tool_outputs(history)
        errors = {key: receipt["error"] for key, receipt in out.items() if "error" in receipt}
        if errors:
            raise ValueError(str(errors))
        request = json.loads(next(item["content"] for item in reversed(history) if item.get("role") == "user"))["request"]
        if request == SEED or "save" in out:
            result = [answer("Done.")]
        elif request == MEASURE:
            if "trial" not in out:
                result = [call("trial", "read_file", file_id=TRIAL, start_line=1, line_count=20)]
            else:
                morning, afternoon = [row for row in out["trial"]["lines"] if row["text"].startswith("Observed throughput:")]
                if "gain" not in out:
                    operands = [{"name": name, "value": re.search(r"\d+", line["text"]).group(),
                                 "source_url": line["evidence_url"], "interpretation": "Observed crates per hour"}
                                for name, line in (("morning", morning), ("afternoon", afternoon))]
                    result = [call("gain", "calculate_with_evidence", expression="100*(afternoon-morning)/morning",
                                   inputs=operands, conditions=[]),
                              call("delta", "calculate_with_evidence", expression="afternoon-morning",
                                   inputs=operands, conditions=[])]
                else:
                    gain = out["gain"]["result"]
                    result = [call("save", "write_artifact", name="result.md", content=(
                        f"{morning['text']} {linked(morning)}\n\n{afternoon['text']} {linked(afternoon)}\n\n"
                        f"Measured gain: {out['delta']['result']} crates per hour ({gain}%)."))]
        elif "find" not in out:
            result = [call("find", "search_saved_work", query="canister transfers second signature",
                           kind="upload", paraphrases=[]),
                      call("note", "read_project_note", scope="Glacier field operations")]
        elif "open" not in out:
            found = next(item for item in out["find"]["candidates"] if item["name"] == HANDBOOK)
            result = [call("open", "open_saved_work", candidate_id=found["candidate_id"])]
        elif "old" not in out:
            result = [call("old", "read_prior_artifact", reference_id=out["open"]["reference_id"],
                           start_line=1, line_count=20)]
            if request == REVISE:
                result.append(call("new", "read_file", file_id=AMENDMENT, start_line=1, line_count=20))
        else:
            result = decision_tools(out, request == REVISE)
        return {"status": "completed", "output": result}


class PdfWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.files = example_files()

    def submit(self, client, prompt, name=None):
        files = [("files", (name, self.files[name], "application/pdf"))] if name else []
        sent = client.post("/tasks", data={"request": prompt}, files=files, follow_redirects=False)
        self.assertEqual(sent.status_code, 303, sent.text)
        url = sent.headers["location"]
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            history = client.get(url + "/history").json()
            if history["status"] == "completed":
                return url, history
            self.assertIn(history["status"], ("queued", "running"), str(history.get("error")))
            time.sleep(.025)
        self.fail(f"Assignment timed out: {url}")

    def check_link(self, client, report, citation, original, expected_page, name):
        self.assertIn(citation, Links(client.get(report).text).hrefs)
        evidence = client.get(urljoin(report, citation))
        self.assertEqual(evidence.status_code, 200)
        self.assertEqual(evidence.url.path, urlsplit(original).path + "/uploads/" + name)
        self.assertEqual(evidence.url.params["page"], str(expected_page))
        anchor = urlsplit(citation).fragment
        self.assertRegex(anchor, r"^L[1-9][0-9]*$")
        self.assertIn(f'id="{anchor}"', evidence.text)
        image = client.get(evidence.url.path + f"/pages/{expected_page}.png")
        self.assertEqual(image.headers["content-type"], "image/png")
        self.assertTrue(image.content.startswith(b"\x89PNG"))
        self.assertEqual(client.get(evidence.url.path, params={"download": 1}).content, self.files[name])

    def test_amended_decision_reopens_both_pdf_pages_and_prior_rationale(self):
        with TestClient(create_app(self.data, PdfWorkflowModel())) as client:
            seed, _ = self.submit(client, SEED, HANDBOOK)
            first, baseline = self.submit(client, POLICY)
            self.assertEqual(baseline["uploads"], [])
            revised, change = self.submit(client, REVISE, AMENDMENT)
            initial = tool_outputs(baseline["steps"])
            updated = tool_outputs(change["steps"])
            self.assertEqual(updated["qualify"]["relationship"], "qualifies")
            self.assertEqual(updated["note"]["revision"], 1)
            initial_source = source_line(initial["old"], "All canister transfers")
            updated_source = source_line(updated["new"], "Sealed canister transfers")
            earlier_source = source_line(updated["old"], "All canister transfers")
            self.assertEqual((initial_source["page"], updated_source["page"]), (2, 3))
            original_report = first + "/artifacts/decision.md"
            new_report = revised + "/artifacts/decision.md"
            reports = [client.get(url, params={"download": 1}).content for url in (original_report, new_report)]
            self.assertIn(b"All canister transfers", reports[0])
            self.assertIn(b"Sealed canister transfers", reports[1])
            self.assertIn(b"all Marsh transfers still require a second signature", reports[1])
            self.assertIn(b"inventory label", reports[1])
            self.assertNotIn(b"For discussion only", reports[1])
        with TestClient(create_app(self.data)) as client:
            self.assertEqual(reports, [client.get(url, params={"download": 1}).content
                                       for url in (original_report, new_report)])
            self.check_link(client, original_report, initial_source["evidence_url"], seed, 2, HANDBOOK)
            self.check_link(client, new_report, updated_source["evidence_url"], revised, 3, AMENDMENT)
            self.check_link(client, new_report, earlier_source["evidence_url"], seed, 2, HANDBOOK)
            for rev, text, line, name, url, page in ((1, "All canister transfers", initial_source, HANDBOOK, seed, 2),
                                                    (2, "Sealed canister transfers", updated_source, AMENDMENT, revised, 3)):
                note = f"/notes/1?revision={rev}"
                self.assertIn(text, client.get(note).text)
                canonical = urlsplit(url).path + f"/uploads/{name}?page={page}#L{line['number']}"
                self.check_link(client, note, canonical, url, page, name)

    def test_observed_values_from_different_pages_feed_a_general_calculation(self):
        with TestClient(create_app(self.data, PdfWorkflowModel())) as client:
            task, history = self.submit(client, MEASURE, TRIAL)
            outputs = tool_outputs(history["steps"])
            report = task + "/artifacts/result.md"
            markdown = client.get(report, params={"download": 1}).text
            self.assertIn("30 crates per hour (25%)", markdown)
            self.assertNotIn("purple", markdown)
            operands = outputs["gain"]["inputs"]
            self.assertEqual([item["value"] for item in operands], ["120", "150"])
            for page, operand in enumerate(operands, 2):
                self.check_link(client, report, operand["source_url"], task, page, TRIAL)
