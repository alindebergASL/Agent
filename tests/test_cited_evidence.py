import unittest
from unittest.mock import Mock

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import run_assignment


class CitedEvidenceTests(unittest.TestCase):
    def test_follow_exact_sources_from_a_note_only_after_receiving_it(self):
        owner = "a" * 32
        pinned = f"/tasks/{owner}/artifacts/policy.md?revision=2#L2"
        connected = f"/tasks/{owner}/evidence/" + "b" * 32
        store = Mock()
        store.read_project_note.return_value = {"scope": "Release", "revision": 1,
            "claims": [{"text": "Release is on hold.", "sources": [pinned, connected]}]}
        store.artifact_revision.return_value = {"content": b"# Release\nHold for review.\n"}
        store.source_evidence.return_value = {"source": "approval system", "tool": "lookup", "data": {"approved": False}}

        def follow(label, url):
            return call(label, "read_cited_evidence", source_url=url, start_line=0, line_count=1)

        model = Mock()
        model.respond.side_effect = [
            {"status": "completed", "output": [call("note", "read_project_note", scope="Release"), follow("early", pinned)]},
            {"status": "completed", "output": [follow("policy", pinned), follow("log", connected),
                                               follow("wrong", pinned.replace("revision=2", "revision=3"))]},
            {"status": "completed", "output": [
                call("refresh", "stage_project_note", scope="Release", expected_revision=1,
                     claims=[{"text": "Release remains on hold.", "sources": ["../prior-artifacts/7#L2"]}],
                     reason="The same approval rule still governs release.")]},
            {"status": "completed", "output": [answer("The release was waiting on approval.")]},
        ]
        records = []
        run_assignment(model, "Explain the release decision.", {}, Mock(), Mock(), records.append,
                       project_note_store=store, task_id="c" * 32, stage_project_note=Mock(),
                       pin_saved_work=Mock(return_value={"reference_id": 7}))
        receipts = tool_outputs(records)
        self.assertIn("error", receipts["early"])
        self.assertIn("error", receipts["wrong"])
        self.assertEqual(receipts["policy"]["lines"], [{"number": 2, "text": "Hold for review."}])
        self.assertEqual(receipts["policy"]["revision_url"], "../prior-artifacts/7")
        self.assertEqual(receipts["log"]["source_url"], connected)
        self.assertEqual(receipts["log"]["data"], {"approved": False})
        self.assertEqual(receipts["log"]["source"], "approval system")
        self.assertTrue(receipts["refresh"]["staged"])
        store.artifact_revision.assert_called_once_with(owner, "policy.md", 2)
