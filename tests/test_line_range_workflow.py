"""HTTP acceptance and reproducible two-record input for bounded source reads."""

import argparse
import re
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from fastapi.testclient import TestClient

from tests.quote_fixture import answer, call, tool_outputs
from tests.test_connected_workflow import Links
from work_agent.web import create_app


REQUEST = ("How many crates were approved across these two delivery records? "
           "Save a short delivery note showing the combined quantity and citing each record.")


def records_bytes():
    lines = []
    for depot, padding, quantity in (("East", "é.", 17), ("West", "📦.", 25)):
        lines.append(f"{depot} dispatch | scan trace: {padding * 8300} | Approved: {quantity} crates.")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


class RangeWorkflowModel:
    """Select calls and write the report from the app's actual range and calculation receipts."""

    def respond(self, history, tools, instructions, **kwargs):
        results = tool_outputs(history)
        if "full" not in results:
            output = [call("full", "read_file", file_id="records.txt", start_line=1, line_count=2)]
        elif "start1" not in results:
            if "error" not in results["full"]:
                raise AssertionError("Expected the ordinary reader to hit the long-line limit.")
            output = [call(f"start{number}", "read_line_range", source={"upload": "records.txt"},
                           line_number=number, start_char=0, char_count=20) for number in (1, 2)]
        elif "tail1" not in results:
            output = [call(f"tail{number}", "read_line_range", source={"upload": "records.txt"},
                           line_number=number, start_char=max(0, results[f"start{number}"]["line_length"] - 64),
                           char_count=64) for number in (1, 2)]
        elif "combined" not in results:
            inputs = []
            for number in (1, 2):
                receipt = results[f"tail{number}"]
                quantity = re.search(r"Approved: (\d+) crates", receipt["text"])
                if quantity is None:
                    raise AssertionError("The returned tail did not expose an approved quantity.")
                inputs.append({"name": f"q{number}", "value": quantity.group(1),
                               "source_url": receipt["evidence_url"], "interpretation": "Approved crates"})
            output = [call("combined", "calculate_with_evidence", expression="q1+q2", inputs=inputs, conditions=[])]
        elif "save" not in results:
            calculation = results["combined"]
            lines = [f"Approved total: {calculation['result']} crates."]
            for number, item in enumerate(calculation["inputs"], 1):
                lines.append(f"[Record {number}]({item['source_url']}): {item['value']} crates.")
            output = [call("save", "write_artifact", name="delivery.md", content="\n\n".join(lines))]
        else:
            output = [answer("Saved the delivery note with both original source lines.")]
        return {"status": "completed", "output": output}


class LineRangeWorkflowTests(unittest.TestCase):
    def test_combine_only_deep_values_and_reopen_exact_source_lines(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        raw = records_bytes()
        lines = raw.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.index("Approved:") > 16000 for line in lines))
        with TestClient(create_app(root, RangeWorkflowModel())) as client:
            sent = client.post("/tasks", data={"request": REQUEST},
                               files={"files": ("records.txt", raw, "text/plain")}, follow_redirects=False)
            self.assertEqual(sent.status_code, 303, sent.text)
            assignment = sent.headers["location"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                history = client.get(assignment + "/history").json()
                if history["status"] == "completed":
                    break
                self.assertIn(history["status"], ("queued", "running"), str(history.get("error")))
                time.sleep(.025)
            self.assertEqual(history["status"], "completed")
            receipts = tool_outputs(history["steps"])
            self.assertIn("16,000", receipts["full"]["error"])
            self.assertEqual(receipts["combined"]["result"], "42")
            self.assertEqual([item["value"] for item in receipts["combined"]["inputs"]], ["17", "25"])
            for number, line in enumerate(lines, 1):
                tail = receipts[f"tail{number}"]
                self.assertGreater(tail["start_char"], 16000)
                self.assertEqual(tail["text"], line[-64:])
                self.assertEqual(tail["end_char"], len(line))
                self.assertIsNone(tail["next_start_char"])
            report = assignment + "/artifacts/delivery.md"
            markdown = client.get(report, params={"download": 1}).content
            self.assertIn(b"Approved total: 42 crates.", markdown)

        with TestClient(create_app(root)) as client:
            self.assertEqual(client.get(report, params={"download": 1}).content, markdown)
            self.assertEqual(client.get(assignment + "/uploads/records.txt", params={"download": 1}).content, raw)
            hrefs = Links(client.get(report).text).hrefs
            for number in (1, 2):
                citation = receipts[f"tail{number}"]["evidence_url"]
                self.assertIn(citation, hrefs)
                target = urlsplit(urljoin(report, citation))
                self.assertEqual(target.fragment, f"L{number}")
                self.assertEqual(target.path, urlsplit(assignment).path + "/uploads/records.txt")
                source = client.get(target.path)
                self.assertEqual(source.status_code, 200)
                self.assertRegex(source.text, rf'<li id="{target.fragment}">[^\n]*<code>{re.escape(lines[number-1])}</code>')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create the two-record delivery acceptance input.")
    parser.add_argument("output", type=Path)
    parser.parse_args().output.write_bytes(records_bytes())
