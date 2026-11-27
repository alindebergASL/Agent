import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.storage import Store
from work_agent.web import create_app


PROPOSAL = {
    "problem": "Two accepted source lines could not be read.",
    "reproduction": "Upload UTF-8 text containing lines over 16,000 characters and read either line.",
    "workaround": "Read the separate summary line to get the requested count.",
    "proposed_change": "Support bounded reads within a long source line.",
    "verification": "Reassemble each original line from its bounded reads without losing characters.",
    "call_ids": ["long-first", "long-second"],
}


class LongRecordModel:
    """Use real file reads; the test double only chooses the next model actions."""

    def __init__(self):
        self.offers = {}

    def respond(self, history, tools, instructions, **kwargs):
        receipts = tool_outputs(history)
        self.offers[tuple(receipts)] = {tool.get("name") for tool in tools}
        if "long-first" not in receipts:
            output = [call("long-first", "read_file", file_id="records.txt", start_line=1, line_count=1)]
        elif "long-second" not in receipts:
            output = [
                call("long-second", "read_file", file_id="records.txt", start_line=2, line_count=1),
                call("too-early", "stage_repair_proposal", **PROPOSAL),
            ]
        elif "summary" not in receipts:
            output = [call("summary", "read_file", file_id="records.txt", start_line=3, line_count=1)]
        elif "stage" not in receipts:
            summary = receipts["summary"]["lines"][0]["text"]
            output = [
                call("write", "write_artifact", name="counts.md", content=summary),
                call("stage", "stage_repair_proposal", **PROPOSAL),
            ]
        else:
            output = [answer("Saved the record count.")]
        return {"status": "completed", "output": output}


class RepairProposalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_completed_assignment_reopens_proposal_receipts_and_requested_artifact(self):
        model = LongRecordModel()
        file_contents = "\n".join(["A" * 16001, "B" * 16002, "Summary: 2 records."])
        with TestClient(create_app(self.root, model)) as client:
            submitted = client.post(
                "/tasks", data={"request": "Read both records and save the summary count."},
                files={"files": ("records.txt", file_contents)}, follow_redirects=False,
            )
            self.assertEqual(submitted.status_code, 303, submitted.text)
            assignment = submitted.headers["location"]
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                history = client.get(assignment + "/history").json()
                if history["status"] in ("failed", "completed"):
                    break
                time.sleep(0.025)
            self.assertEqual(history["status"], "completed", history.get("error"))
            receipts = tool_outputs(history["steps"])
            for call_id in PROPOSAL["call_ids"]:
                self.assertIn("16,000", receipts[call_id]["error"])
            self.assertNotIn("stage_repair_proposal", model.offers[()])
            self.assertNotIn("stage_repair_proposal", model.offers[("long-first",)])
            self.assertIn("error", receipts["too-early"])
            self.assertIn("stage_repair_proposal", model.offers[("long-first", "long-second", "too-early")])
            self.assertTrue(receipts["stage"]["staged_repair_proposal"])

        with TestClient(create_app(self.root)) as client:
            self.assertIn("/repair-proposals", client.get("/").text)
            review = client.get("/repair-proposals")
            self.assertEqual(review.status_code, 200)
            for field in ("problem", "reproduction", "workaround", "proposed_change", "verification"):
                self.assertIn(PROPOSAL[field], review.text)
            proposal, = Store(self.root).list_repair_proposals()
            self.assertIn(proposal["history_url"], review.text)
            trace = client.get(proposal["history_url"]).json()
            source_receipts = tool_outputs(trace["steps"])
            for evidence in proposal["evidence"]:
                self.assertEqual(evidence["tool"], "read_file")
                self.assertEqual(source_receipts[evidence["call_id"]]["error"], evidence["error"])
                self.assertIn(evidence["call_id"], review.text)
            self.assertEqual({entry["call_id"] for entry in proposal["evidence"]}, set(PROPOSAL["call_ids"]))
            saved = client.get(assignment + "/artifacts/counts.md?download=1")
            self.assertEqual(saved.text, "Summary: 2 records.")

    def test_guards_receipt_ownership_and_discards_stale_proposal(self):
        store = Store(self.root)
        store.initialize()

        def begin():
            task = store.submit("Count records.", {})
            self.assertEqual(store.claim(), task)
            turn_id = store.run_history(task)["turns"][-1]["id"]
            return task, turn_id

        def record_error(task, turn_id, call_id, tool="read_file", result=None):
            read = call(call_id, tool, file_id="records.txt", start_line=1, line_count=1)
            store.record_step(task, {"type": "response", "response": {"output": [read]}}, turn_id=turn_id)
            store.record_step(task, {
                "type": "function_call_output", "call_id": call_id,
                "output": json.dumps(result or {"error": "Line 1 exceeds the 16,000-character read limit."}),
            }, turn_id=turn_id)

        task, turn = begin()
        record_error(task, turn, "first-run-only")
        store.complete(task, "Finished.", turn_id=turn)

        for discard in ("stop", "fail", "correct"):
            task, turn = begin()
            record_error(task, turn, "long-first")
            record_error(task, turn, "other-tool", tool="read_saved_artifact")
            record_error(task, turn, "success", result={"lines": [{"number": 3, "text": "Count: 2"}]})
            for other in ("long-first", "first-run-only", "success", "other-tool"):
                invalid = ["long-first", other]
                with self.assertRaises(ValueError):
                    store.stage_repair_proposal(task, turn, **{**PROPOSAL, "call_ids": invalid})
            record_error(task, turn, "long-second")
            store.stage_repair_proposal(task, turn, **PROPOSAL)
            store.stage_repair_proposal(task, turn, **PROPOSAL)
            self.assertEqual(store.list_repair_proposals(), [])
            if discard == "stop":
                store.stop_turn(task, turn)
            elif discard == "fail":
                store.fail(task, "Interrupted before publishing.", turn_id=turn)
            else:
                store.correct_turn(task, turn, "Use a new record.")
                self.assertEqual(store.claim(), task)
                store.complete(task, "Finished the correction.")
            self.assertEqual(store.list_repair_proposals(), [])
