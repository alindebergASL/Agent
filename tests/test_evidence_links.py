import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from tests.quote_fixture import answer, call, tool_outputs
from tests.test_prior_runner import response
from work_agent.runner import run_assignment
from work_agent.storage import Store
from work_agent.web import create_app


class EvidenceLinkTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.data)
        self.store.initialize()

    def test_exact_citations_and_reads_are_separate_across_revisions_and_restart(self):
        source = self.store.submit("Review travel policy", {"rule#1.txt": b"Scope\nTravel requires approval.\n"})
        self.store.claim()
        self.store.stage(source, "decision.md", "Apply [line 2](../uploads/rule%231.txt#L2).")
        self.store.stage(source, "terms.csv", "field,value\nstatus,approved")
        self.store.complete(source, "Done")
        self.store.save_markdown(source, "decision.md", """
See `[a literal](../uploads/rule%231.txt#L2)`, [absent](../uploads/rule%231.txt#L3),
or [another](../uploads/missing#L2).
""", 1)
        remote = self.store.submit("Change travel policy", {"rule#1.txt": b"scope\nTravel is exempt."},
                                   prior_revision=(source, "rule#1.txt", None))
        self.store.claim()
        turn_id = self.store.get_task(remote)["turns"][-1]["id"]
        pin, = self.store.get_task(remote)["prior_artifacts"]
        saved = self.store.pin_saved_work(remote, source, "terms.csv", 1)
        self.store.stage(remote, "out.md", f"[new](../uploads/rule%231.txt#L2) [terms](../prior-artifacts/{saved['reference_id']}#L2)")
        self.store.record_step(remote, {"type": "response", "response": response(
            call("p1", "read_prior_artifact", reference_id=pin["reference_id"], start_line=2, line_count=1))})
        self.store.record_step(remote, {"type": "function_call_output", "call_id": "p1", "output": json.dumps(
            {"file_id": "rule#1.txt", "lines": [{"number": 2, "text": "Travel requires approval."}]})})
        current_url = f"/tasks/{remote}/uploads/rule%231.txt"
        self.store.record_step(remote, {"type": "response", "response": response(
            call("c1", "read_cited_evidence", source_url=f"{current_url}#L2", start_line=2, line_count=1))})
        self.store.record_step(remote, {"type": "function_call_output", "call_id": "c1", "output": json.dumps(
            {"file_id": "rule#1.txt", "source_task_id": remote, "source_url": current_url, "revision": None,
             "lines": [{"number": 2, "text": "Travel is exempt."}]})})
        self.store.stage_project_note(remote, turn_id, "Travel", 0,
                                      [{"text": "Approval is required.", "sources": [f"../prior-artifacts/{pin['reference_id']}#L2"]}], "")
        self.store.complete(remote, "Done")
        unconsulted = self.store.submit("Other reason", {}, prior_revision=(source, "rule#1.txt", None))
        with self.store._connection() as db:
            db.execute("DROP TRIGGER index_note_evidence")
            for table in ("note_claim_citations", "artifact_citations", "evidence_reads"):
                db.execute(f"DROP TABLE {table}")
        self.store.initialize()
        results = self.store.find_evidence_uses(remote, pin["reference_id"])
        self.assertEqual(results["citing_revisions"], [{"source_task_id": source, "name": "decision.md",
                         "revision": 1, "current": False, "lines": [2], "same_assignment": False,
                         "revision_url": f"/tasks/{source}/artifacts/decision.md?revision=1"}])
        readers = {r["task_id"]: (r["pinned"], r["read"]) for r in results["related_assignments"]}
        self.assertEqual(readers, {remote: (True, True), unconsulted: (True, False)})
        claim, = results["note_claims"]
        self.assertEqual(claim["claim"]["text"], "Approval is required.")
        self.assertIn("?revision=1#claim-1", claim["note_url"])
        own = self.store.evidence_uses(remote, "rule#1.txt", None)
        self.assertEqual(own["citing_revisions"][0]["name"], "out.md")
        self.assertEqual(own["related_assignments"], [{"task_id": remote, "pinned": False, "read": True}])
        self.assertEqual(self.store.evidence_uses(source, "terms.csv", 1)["citing_revisions"][0]["lines"], [2])

        self.store.claim()
        self.store.complete(unconsulted, "Done.")
        with TestClient(create_app(self.data, Mock())) as browser:
            page = f"/tasks/{source}/evidence-uses/upload/rule%231.txt"
            self.assertIn("Where this original was used", browser.get(f"/tasks/{source}/uploads/rule%231.txt").text)
            html = browser.get(page).text
            self.assertIn(f"/tasks/{source}/artifacts/decision.md?revision=1", html)
            self.assertIn("historical", html)
            self.assertEqual(browser.get(f"/tasks/{source}/artifacts/decision.md?revision=1").text.count('id="L1"'), 1)
            self.assertIn("Approval is required.", html)
            self.assertEqual(browser.get(f"/tasks/{source}/evidence-uses/artifact/terms.csv?revision=9").status_code, 404)

    def test_missing_legacy_upload_is_indexed_only_when_the_original_returns(self):
        original = self.store.submit("Summarize policy", {"policy.txt": b"scope\nOriginal condition."})
        self.store.claim()
        self.store.stage(original, "report.md", "[Condition](../uploads/policy.txt#L2) [absent](../uploads/policy.txt#L3)")
        self.store.complete(original, "Done")
        with self.store._connection() as db:
            pos = db.execute("UPDATE uploads SET content=NULL WHERE task_id=? RETURNING position", (original,)).fetchone()[0]
            for table in ("artifact_citations", "evidence_reads", "pending_upload_citations"):
                db.execute(f"DROP TABLE {table}")
        self.store.initialize()
        self.store.initialize()
        self.assertIsNone(self.store.read_upload(original, "policy.txt"))
        self.assertEqual(self.store.evidence_uses(original, "policy.txt", None)["citing_revisions"], [])
        legacy_path = self.data / "runs" / original / "inputs" / str(pos)
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_bytes(b"scope\nOriginal condition.")
        # A lookup after restore works even while the service remains up.
        report, = self.store.evidence_uses(original, "policy.txt", None)["citing_revisions"]
        self.assertEqual((report["name"], report["lines"]), ("report.md", [2]))
        self.store.initialize()
        self.assertEqual(self.store.evidence_uses(original, "policy.txt", None)["citing_revisions"], [report])

    def test_two_hop_policy_review_tracks_the_exact_intermediate_revision_and_stops_cycles(self):
        source = self.store.submit("Prepare the approval checklist", {"policy.txt": b"Production must be approved."})
        self.store.claim()
        self.store.stage(source, "checklist.md", "[Approval](../uploads/policy.txt#L1) and [appendix](../artifacts/appendix.md?revision=1).")
        self.store.stage(source, "appendix.md", "[Checklist](../artifacts/checklist.md?revision=1#L1).")
        self.store.complete(source, "Saved.")
        self.store.save_markdown(source, "checklist.md", "A newer checklist without the original policy.", 1)

        rollout = self.store.submit("Approve deployment", {}, prior_revision=(source, "checklist.md", 1))
        self.store.claim()
        p1, = self.store.get_task(rollout)["prior_artifacts"]
        p2 = self.store.pin_saved_work(rollout, source, "checklist.md", 2)
        self.store.stage(rollout, "decision.md", f"Approve after review of [checkpoint](../prior-artifacts/{p1['reference_id']}#L1).")
        self.store.stage(rollout, "other.md", f"Different rule [here](../prior-artifacts/{p2['reference_id']}#L1).")
        self.store.complete(rollout, "Saved.")

        trace = self.store.evidence_uses(source, "policy.txt", None)
        self.assertEqual([(r["name"], r["revision"]) for r in trace["citing_revisions"]], [("checklist.md", 1)])
        indirect = {r["name"]: r for r in trace["indirect_review_candidates"]}
        self.assertEqual(set(indirect), {"decision.md", "appendix.md"})
        path = indirect["decision.md"]
        self.assertEqual(path["via"]["revision_url"], f"/tasks/{source}/artifacts/checklist.md?revision=1")
        self.assertEqual(path["revision_url"], f"/tasks/{rollout}/artifacts/decision.md?revision=1")
        self.assertEqual((path["via"]["lines"], path["lines"]), ([1], [1]))
        cycle = self.store.evidence_uses(source, "checklist.md", 1)
        self.assertNotIn("checklist.md", [r["name"] for r in cycle["indirect_review_candidates"]])

    def test_followup_traces_active_assignment_files_without_pinning_itself(self):
        source = self.store.submit("Review the policy", {"policy.txt": b"Use two reviewers."})
        self.store.claim()
        self.store.stage(source, "review.md", "[Policy](../uploads/policy.txt#L1)")
        self.store.complete(source, "Saved")
        reads = []
        model = Mock()
        model.respond.side_effect = [
            response(call("local", "find_evidence_uses", source={"upload": "policy.txt"}, offset=0),
                     call("own", "find_evidence_uses", source={"artifact": "review.md", "revision": 1}, offset=0),
                     call("future", "find_evidence_uses", source={"artifact": "review.md", "revision": 2}, offset=0),
                     call("unknown", "find_evidence_uses", source={"upload": "other.txt"}, offset=0)),
            response(answer("Reviewed.")),
        ]
        run_assignment(model, "What relies on this file?", {"policy.txt": "Use two reviewers."}, Mock(), Mock(), reads.append,
                       saved_artifacts={"review.md": {"revision": 1}}, read_prior_artifact=Mock(),
                       search_saved_work=Mock(), pin_saved_work=Mock(), find_evidence_uses=lambda which, offset:
                       self.store.find_evidence_uses(source, which, offset))
        replies = tool_outputs(reads)
        link, = replies["local"]["citing_revisions"]
        self.assertEqual((link["name"], link["same_assignment"]), ("review.md", True))
        self.assertNotIn("candidate_id", link)
        self.assertEqual(replies["own"]["citing_revisions"], [])
        self.assertIn("error", replies["future"])
        self.assertIn("error", replies["unknown"])
        self.assertEqual(self.store.get_task(source)["prior_artifacts"], [])

    def test_evidence_trace_returns_only_delivered_reopenable_candidates(self):
        ref = {"reference_id": 8, "source_task_id": "src", "name": "terms.txt", "revision": None}
        after = {"reference_id": 9, "source_task_id": "report", "name": "report.md", "revision": 1}
        model = Mock()
        model.respond.side_effect = [
            response(call("guess", "find_evidence_uses", source=99)),
            response(call("trace", "find_evidence_uses", reference_id=8),
                     call("early", "open_saved_work", candidate_id=1)),
            response(call("open", "open_saved_work", candidate_id=1)),
            response(answer("Found it.")),
        ]
        records = []
        trace = Mock(return_value={"citing_revisions": [{**after, "revision_url": "/tasks/report/artifacts/report.md?revision=1"}]})
        pin = Mock(return_value=after)
        run_assignment(model, "Trace this decision.", {}, Mock(), Mock(), records.append,
                       prior_artifacts=[ref], read_prior_artifact=Mock(), search_saved_work=Mock(),
                       pin_saved_work=pin, find_evidence_uses=trace)
        outputs = tool_outputs(records)
        self.assertIn("error", outputs["guess"])
        self.assertIn("error", outputs["early"])
        self.assertEqual(outputs["trace"]["citing_revisions"][0]["candidate_id"], 1)
        pin.assert_called_once_with("report", "report.md", 1)
