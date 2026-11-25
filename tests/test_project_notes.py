import html
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import run_assignment
from work_agent.storage import InactiveTurn, RevisionConflict, Store
from work_agent.web import create_app


class ProjectNoteTests(unittest.TestCase):
    scope = "Pine Harbor migration"
    policy = "policy #1?.txt"
    alias = "../uploads/policy%20%231%3F.txt#L2"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.store = Store(self.path)
        self.store.initialize()

    def start(self, uploads):
        task = self.store.submit("Review the migration policy.", uploads)
        self.assertEqual(self.store.claim(), task)
        return task, self.store.get_run(task)["turn_id"]

    def run_tools(self, actions, task, turn):
        records = []
        model = Mock()
        model.respond.side_effect = [{"status": "completed", "output": row} for row in actions]
        run_assignment(model, "Maintain the Pine Harbor policy.", self.store.read_uploads(task),
                       Mock(), Mock(), records.append, project_note_store=self.store,
                       stage_project_note=lambda **args: self.store.stage_project_note(task, turn, **args))
        return tool_outputs(records)

    def test_amended_policy_changes_current_decision_and_explains_old_one(self):
        original, turn = self.start({self.policy: b"April policy\nSecurity director must approve releases.\n"})
        old_text = "Obtain the security director's approval before release."
        new_text = "The project owner can now approve releases independently."
        first = {"text": old_text, "sources": [self.alias]}
        self.store.stage_project_note(original, turn, self.scope, 0, [first], "The April policy requires director review.")
        self.store.complete(original, "Await director approval.")
        amendment, turn = self.start({"signed.txt": b"Signed May amendment\nProject owner may approve independently.\n"})
        old = self.store.read_project_note(self.scope)
        new_link = "../uploads/signed.txt#L2"
        new = {"text": new_text, "sources": [new_link, old["claims"][0]["sources"][0]], "supersedes": 1}
        reason = "The signed May amendment delegates approval to the project owner. The April policy required the director."
        actions = [
            [call("discover", "search_project_notes", query="Who approves releases at Pine Harbor now?")],
            [call("read", "read_project_note", scope=self.scope), call("new", "read_file", file_id="signed.txt",
                                                                      start_line=2, line_count=1)],
            [call("publish", "stage_project_note", scope=self.scope, expected_revision=1, claims=[new], reason=reason)],
            [answer(new_text)],
        ]
        receipts = self.run_tools(actions, amendment, turn)
        self.assertTrue(receipts["publish"].get("staged"), receipts["publish"])
        self.assertEqual(receipts["discover"]["notes"][0]["scope"], self.scope)
        self.assertNotIn("claims", receipts["discover"]["notes"][0])
        self.store.complete(amendment, new_text)
        with TestClient(create_app(data_dir=self.path)) as client:
            current = client.get("/notes/1")
            self.assertIn(new_text, current.text)
            self.assertIn(reason, current.text)
            self.assertIn('?revision=1#claim-1', current.text)
            self.assertIn(f'/tasks/{amendment}/uploads/signed.txt#L2', current.text)
            self.assertIn(f'/tasks/{original}/uploads/policy%20%231%3F.txt#L2', current.text)
            history = client.get("/notes/1?revision=1")
            self.assertIn(old_text, html.unescape(history.text))
            self.assertNotIn(new_text, history.text)
            self.assertIn("Security director must approve", client.get(old["claims"][0]["sources"][0]).text)
        third, turn = self.start({})
        earlier = self.run_tools([[call("past", "read_project_note_revision", scope=self.scope, revision=1)],
                                  [answer(old_text)]], third, turn)
        self.assertEqual(earlier["past"]["claims"], old["claims"])

    def test_same_batch_or_unread_line_cannot_support_note(self):
        task, turn = self.start({self.policy: b"Heading\nrule\n"})
        data = {"scope": self.scope, "expected_revision": 0, "claims": [{"text": "rule", "sources": [self.alias]}]}
        result = self.run_tools([
            [call("note", "read_project_note", scope=self.scope),
             call("heading", "read_file", file_id=self.policy, start_line=1, line_count=1),
             call("racing", "stage_project_note", **data)],
            [call("unread", "stage_project_note", **data)],
            [call("rule", "read_file", file_id=self.policy, start_line=2, line_count=1)],
            [call("valid", "stage_project_note", **data)], [answer("Ready.")],
        ], task, turn)
        self.assertIn("earlier response", result["racing"]["error"])
        self.assertIn("source lines", result["unread"]["error"])
        self.assertTrue(result["valid"]["staged"])
        self.store.stop_turn(task, turn)
        with self.assertRaises(InactiveTurn):
            self.store.complete(task, "Stopped", turn_id=turn)
        self.assertEqual(self.store.read_project_note(self.scope)["revision"], 0)

    def test_concurrent_user_edit_preserved_and_other_artifacts_rollback(self):
        task, turn = self.start({self.policy: b"header\nrule"})
        claim = {"text": "Initial rule", "sources": [self.alias]}
        self.store.stage_project_note(task, turn, self.scope, 0, [claim])
        self.store.complete(task, "Original.")
        # Another assignment revises the same project while a user edits the note.
        next_task, next_turn = self.start({})
        canonical = self.store.read_project_note(self.scope)["claims"][0]["sources"][0]
        self.store.stage_project_note(next_task, next_turn, self.scope, 1,
                                      [{"text": "Stale rule", "sources": [canonical]}], "Proposed clarification")
        self.store.stage(next_task, "stale.md", "Draft", turn_id=next_turn)
        # Keep the running assignment intact by writing via the same Store used by the UI.
        self.store.edit_project_note(1, 1, [{"text": "User's clarification", "sources": [canonical]}], "Human correction")
        with self.assertRaises(RevisionConflict):
            self.store.complete(next_task, "Stale result.")
        self.store.fail(next_task, "Conflict.", turn_id=next_turn)
        self.assertEqual(self.store.read_project_note(self.scope)["revision"], 2)
        self.assertIsNone(self.store.artifact_revision(next_task, "stale.md"))
        with TestClient(create_app(data_dir=self.path)) as client:
            fields = {"claim": "Updated wording", "sources": canonical, "supersedes": "1", "reason": "Clarify scope"}
            self.assertEqual(client.post("/notes/1", data={**fields, "expected_revision": "1"}).status_code, 409)
            self.assertEqual(client.post("/notes/1", data={**fields, "expected_revision": "2"},
                                         follow_redirects=False).status_code, 303)
            self.assertIn("Updated wording", client.get("/notes/1").text)
            self.assertIn("User&#39;s clarification", client.get("/notes/1?revision=2").text)

    def test_invalid_sources_and_correction_or_restart_discard_pending_note(self):
        for action in ("correct", "interrupt"):
            with self.subTest(action=action):
                task, turn = self.start({self.policy: b"one\ntwo\n"})
                for source in ("https://example.org", self.alias[:-1] + "9", "../uploads/missing#L1"):
                    with self.assertRaises(ValueError):
                        self.store.stage_project_note(task, turn, self.scope, 0, [{"text": "Rule", "sources": [source]}])
                self.store.stage_project_note(task, turn, self.scope, 0, [{"text": "Rule", "sources": [self.alias]}])
                if action == "correct":
                    new = self.store.correct_turn(task, turn, "Use signed terms.")
                    self.store.stop_turn(task, new)
                else:
                    self.store.initialize()
                self.assertEqual(self.store.read_project_note(self.scope)["revision"], 0)
                with self.store._connection() as connection:
                    self.assertEqual(connection.execute("SELECT count(*) FROM staged_project_notes").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
