"""Read a policy amendment and trace its effect on later decisions across assignments."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.storage import InactiveTurn, Store
from work_agent.web import create_app


POLICY = b"Access policy\nVisitor entry requires a manager signature.\nOperators must complete orientation.\n"
AMENDMENT = b"Approved exception\nNorth lab visitors may enter escorted without a manager signature.\n"
DRAFT = b"Unapproved proposal\nDrop the signature requirement for visitors everywhere.\n"
BASE = "Record our North lab access decision and cite its authority."
CHANGE = "Reassess the North lab decision under the amendment. Check whether East is affected."
AUDIT = "What changed in the visitor rule? Check orientation too."


NOTE_AUDIT = "Consult the facility access note. Which source changed?"


class PolicyFixture:
    def respond(self, history, tools, instructions, *, max_tool_calls):
        user = json.loads(next(x for x in reversed(history) if x.get("role") == "user")["content"])
        request = user["request"]
        out = tool_outputs(history)
        if request == BASE and "rule" not in out:
            items = [call("rule", "read_file", file_id="handbook.txt", start_line=2, line_count=2),
                     call("note", "read_project_note", scope="Facility access")]
        elif request == BASE and "save" not in out:
            claims = [{"text": "All visitors require a manager signature.",
                       "sources": ["../uploads/handbook.txt#L2"], "supersedes": None},
                      {"text": "Orientation is required.", "sources": ["../uploads/handbook.txt#L3"],
                       "supersedes": None}]
            items = [call("save", "write_artifact", name="decision.md",
                          content="Require a manager signature for North. [Policy](../uploads/handbook.txt#L2)"),
                     call("keep_note", "stage_project_note", scope="Facility access",
                          expected_revision=0, claims=claims, reason="Original handbook rules.")]
        elif request == CHANGE and "old" not in out:
            items = [
                call("old", "read_prior_artifact", reference_id=user["prior_artifacts"][0]["reference_id"],
                     start_line=2, line_count=1),
                call("new", "read_file", file_id="amendment.txt", start_line=2, line_count=1),
                call("draft", "read_file", file_id="proposal.txt", start_line=2, line_count=1),
                call("too_early", "stage_evidence_change",
                     earlier_source=f"../prior-artifacts/{user['prior_artifacts'][0]['reference_id']}#L2",
                     new_source="../uploads/amendment.txt#L2", relationship="qualifies", scope="North", reason="Signed."),
            ]
        elif request == CHANGE and "qualify" not in out:
            old = out["old"]["revision_url"] + "#L2"
            items = [
                call("qualify", "stage_evidence_change", earlier_source=old,
                     new_source="../uploads/amendment.txt#L2", relationship="qualifies", scope="Escorted North visitors",
                     reason="The approved exception waives the signature only on escorted North visits."),
                call("conflict", "stage_evidence_change", earlier_source=old,
                     new_source="../uploads/proposal.txt#L2", relationship="contradicts", scope="Unapproved general proposal",
                     reason="The proposal conflicts with the published rule and has no approval."),
                call("save", "write_artifact", name="decision.md", content=(
                    f"North permits escorted entry. [Amendment](../uploads/amendment.txt#L2) "
                    f"East still requires a signature. [Handbook]({old})"
                )),
            ]
        elif request == NOTE_AUDIT and "current_note" not in out:
            items = [call("current_note", "read_project_note", scope="Facility access"),
                     call("historical_note", "read_project_note_revision", scope="Facility access", revision=1)]
        elif request == NOTE_AUDIT and "new_evidence" not in out:
            rows = out["current_note"]["evidence_changes"]["changes"]
            amendment = next(row for row in rows if row["relationship"] == "qualifies")
            items = [call("new_evidence", "read_cited_evidence", source_url=amendment["new_source"],
                          start_line=0, line_count=1)]
        elif request == AUDIT and "visitors" not in out:
            ref = user["prior_artifacts"][0]["reference_id"]
            items = [
                call("visitors", "read_prior_artifact", reference_id=ref, start_line=2, line_count=1),
                call("orientation", "read_prior_artifact", reference_id=ref, start_line=3, line_count=1),
            ]
        elif request == AUDIT and "verified_exception" not in out:
            changes = out["visitors"]["evidence_changes"]["changes"]
            exception = next(change for change in changes if change["relationship"] == "qualifies")
            items = [call("verified_exception", "read_cited_evidence", source_url=exception["new_source"],
                          start_line=2, line_count=1)]
        else:
            items = [answer("Review complete.")]
        return {"status": "completed", "output": items}


class PolicyChangeTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.data)
        self.store.initialize()

    def submit(self, client, request, files=None, path="/tasks"):
        post = client.post(path, data={"request": request}, files=files, follow_redirects=False)
        self.assertEqual(post.status_code, 303, post.text)
        url = post.headers["location"]
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            history = client.get(url + "/history").json()
            if history["status"] == "completed":
                return url, tool_outputs(history["steps"])
            self.assertIn(history["status"], ("running", "queued"), history.get("error"))
            time.sleep(.025)
        self.fail("Timed out waiting for assignment")

    def test_cross_assignment_policy_exception_and_unresolved_conflict(self):
        with TestClient(create_app(self.data, PolicyFixture())) as client:
            original, _ = self.submit(client, BASE, [("files", ("handbook.txt", POLICY))])
            # Reuse the original handbook through the ordinary new-assignment path.
            form = original + "/uploads/handbook.txt/new-assignment"
            revised, out = self.submit(client, CHANGE, [
                ("files", ("amendment.txt", AMENDMENT)), ("files", ("proposal.txt", DRAFT)),
            ], path=form)
            self.assertIn("error", out["too_early"])
            self.assertEqual(out["qualify"]["relationship"], "qualifies")
            self.assertEqual(out["conflict"]["relationship"], "contradicts")
            old_report = client.get(original + "/artifacts/decision.md", params={"download":1}).text
            self.assertIn("Require a manager signature for North", old_report)
            new_report = client.get(revised + "/artifacts/decision.md", params={"download":1}).text
            self.assertIn("North permits escorted entry", new_report)
            self.assertIn("East still requires a signature", new_report)

        with TestClient(create_app(self.data, PolicyFixture())) as client:
            _, note_out = self.submit(client, NOTE_AUDIT)
            observed = note_out["current_note"]
            self.assertEqual(observed["revision"], 1)
            self.assertEqual(observed["claims"], note_out["historical_note"]["claims"])
            self.assertNotIn("evidence_changes", note_out["historical_note"])
            self.assertFalse(observed["evidence_changes"]["truncated"])
            self.assertCountEqual([row["relationship"] for row in observed["evidence_changes"]["changes"]],
                                  ["qualifies", "contradicts"])
            for row in observed["evidence_changes"]["changes"]:
                self.assertEqual(row["claim_number"], 1)
                self.assertEqual(row["cited_source"], observed["claims"][0]["sources"][0])
            self.assertEqual(note_out["new_evidence"]["lines"][0]["text"], AMENDMENT.decode().splitlines()[1])
            _, out = self.submit(client, AUDIT, path=form)
            changes = out["visitors"]["evidence_changes"]["changes"]
            self.assertCountEqual([x["relationship"] for x in changes], ["qualifies", "contradicts"])
            self.assertNotIn("evidence_changes", out["orientation"])
            self.assertEqual(out["verified_exception"]["lines"][0]["text"], AMENDMENT.decode().splitlines()[1])
            self.assertEqual(out["verified_exception"]["source_task_id"], revised.rsplit("/", 1)[1])
            for change in changes:
                self.assertEqual(change["earlier_source"], original.removeprefix("http://testserver") + "/uploads/handbook.txt#L2")
                self.assertEqual(change["assignment_url"], revised.removeprefix("http://testserver"))
                self.assertEqual(client.get(change["new_source"]).status_code, 200)
                self.assertEqual(client.get(change["earlier_source"]).status_code, 200)
            self.assertEqual(client.get(original + "/artifacts/decision.md", params={"download":1}).text, old_report)

    def test_only_completed_turns_publish_and_sources_must_be_distinct(self):
        for status in ("stop", "correct", "restart", "fail", "complete"):
            with self.subTest(status=status):
                store = Store(self.data / status)
                store.initialize()
                task = store.submit("Reassess", {"old": POLICY, "new": AMENDMENT})
                store.claim()
                turn = store.get_run(task)["turn_id"]
                args = [task, turn, "../uploads/new#L2", "../uploads/old#L2", "qualifies", "North", "Escorted visits."]
                with self.assertRaises(ValueError):
                    store.stage_evidence_change(*[task, turn, args[2], args[2], *args[4:]])
                store.stage_evidence_change(*args)
                self.assertEqual(store.evidence_changes(task, "old", None)["changes"], [])
                if status == "stop":
                    store.stop_turn(task, turn)
                elif status == "correct":
                    store.correct_turn(task, turn, "Use a corrected instruction.")
                elif status == "restart":
                    store.initialize()
                elif status == "fail":
                    store.fail(task, "Interrupted review", turn_id=turn)
                else:
                    store.complete(task, "Done", turn_id=turn)
                with self.assertRaises(InactiveTurn):
                    store.stage_evidence_change(*args)
                for name in ("old", "new"):
                    self.assertEqual(len(store.evidence_changes(task, name, None)["changes"]), int(status == "complete"))

    def test_supersession_is_specific_to_a_published_revision_and_effective_scope(self):
        source = self.store.submit("Publish the approval matrix", {})
        self.store.claim()
        self.store.stage(source, "matrix.md", "# Procedure\nA director must approve.\n")
        self.store.complete(source, "Published.")
        self.store.save_markdown(source, "matrix.md", "# Procedure\nA manager may approve.\n", 1)
        task = self.store.submit("Record the new delegated authority", {})
        self.store.claim()
        turn = self.store.get_run(task)["turn_id"]
        v1 = self.store.pin_saved_work(task, source, "matrix.md", 1)
        v2 = self.store.pin_saved_work(task, source, "matrix.md", 2)
        self.store.stage_evidence_change(
            task, turn, f"../prior-artifacts/{v2['reference_id']}#L2", f"../prior-artifacts/{v1['reference_id']}#L2",
            "supersedes", "Routine purchases after the delegation date", "Authority moved from director to manager.",
        )
        self.store.complete(task, "Published.")
        expected_new = f"/tasks/{source}/artifacts/matrix.md?revision=2#L2"
        expected_old = f"/tasks/{source}/artifacts/matrix.md?revision=1#L2"
        for rev in (1, 2):
            change, = self.store.evidence_changes(source, "matrix.md", rev)["changes"]
            self.assertEqual((change["new_source"], change["earlier_source"]), (expected_new, expected_old))
            self.assertEqual(change["relationship"], "supersedes")
            self.assertEqual(self.store.evidence_changes(source, "matrix.md", rev, start_line=1, end_line=1)["changes"], [])
        self.store.save_markdown(source, "matrix.md", "# Procedure\nAn executive must approve.\n", 2)
        self.assertEqual(self.store.evidence_changes(source, "matrix.md", 3)["changes"], [])

    def test_note_change_summary_has_a_page_bound_and_gives_each_affected_claim_a_turn(self):
        task = self.store.submit("Record conflicting reports", {"earlier": POLICY, "later": AMENDMENT})
        self.store.claim()
        turn = self.store.get_run(task)["turn_id"]
        for number in range(20):
            self.store.stage_evidence_change(
                task, turn, "../uploads/later#L2", "../uploads/earlier#L2", "qualifies",
                f"Facility {number}", "Escorted entry approved.",
            )
        self.store.stage_evidence_change(
            task, turn, "../uploads/later#L1", "../uploads/earlier#L3", "contradicts",
            "Orientation", "Published sources disagree.",
        )
        self.store.complete(task, "Recorded.")
        base = f"/tasks/{task}/uploads/"
        claims = [{"sources": [base+"earlier#L2"]}, {"sources": [base+"later#L1", base+"earlier#L3"]}]
        result = self.store.changes_for_note_claims(claims)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["changes"]), 20)
        self.assertEqual([row["claim_number"] for row in result["changes"][:2]], [1, 2])
        self.assertEqual(sum(row["claim_number"] == 2 for row in result["changes"]), 1)
        self.assertEqual(self.store.changes_for_note_claims([{"sources": [base+"later#L2"]}]), {})
