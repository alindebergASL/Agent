import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.memory_context import PREVIEW_BYTES, encoded_size
from work_agent.storage import Store
from work_agent.web import create_app


class MemoryReader:
    """Use the application's first-turn context and reopen its evidence without a note search."""

    def respond(self, history, tools, instructions, *, max_tool_calls):
        results = tool_outputs(history)
        if "exception" in results:
            line = results["exception"]["lines"][0]
            output = answer(f"Instructions for the crew: {line['text']}")
        else:
            request = json.loads(history[0]["content"])
            claim = request["relevant_memory"]["claims"][0]
            source = claim["evidence_changes"][0]["new_source"]
            output = call("exception", "read_cited_evidence", source_url=source, start_line=0, line_count=1)
        return {"status": "completed", "output": [output]}


class ProactiveMemoryTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.path)
        self.store.initialize()

    def seed(self, scope, claim):
        task = self.store.submit("Record the current practice.", {"terms.txt": claim.encode()})
        self.assertEqual(self.store.claim(), task)
        self.store.stage_project_note(task, self.store.get_run(task)["turn_id"], scope, 0,
                                      [{"text": claim, "sources": ["../uploads/terms.txt#L1"]}])
        self.store.complete(task, "Recorded.")
        return task

    def test_current_scope_and_operative_exception_are_offered_before_action(self):
        scope = "Meridian Hall"
        term = "All crew should enter through the south door with a coordinator."
        source = self.seed(scope, term)
        self.seed("Meridian Annex", "All crew should enter through the east door with a coordinator.")
        old = self.store.read_project_note(scope)
        claims = [{"text": "Original handbook requires a coordinator for crew entry.",
                   "sources": old["claims"][0]["sources"]}]
        self.store.edit_project_note(old["id"], 1, claims, "Clarify the handbook wording.")
        amendment = self.store.submit("Record approved night instructions.", {
            "night.txt": b"Approved only for night crew: use the west entrance without a coordinator."
        }, prior_revision=(source, "terms.txt", None))
        self.assertEqual(self.store.claim(), amendment)
        turn = self.store.get_run(amendment)["turn_id"]
        self.store.stage_evidence_change(amendment, turn, "../uploads/night.txt#L1", old["claims"][0]["sources"][0],
                                         "qualifies", "Night crew", "The signed instruction is an exception for this shift.")
        self.store.complete(amendment, "Amendment recorded.")

        with TestClient(create_app(self.path, MemoryReader())) as client:
            response = client.post("/tasks", data={"request": "Give the Meridian Hall night crew their arrival instructions."},
                                   follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            location = response.headers["location"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                result = client.get(location + "/history").json()
                if result["status"] not in ("queued", "running"):
                    break
                time.sleep(.02)
            self.assertEqual(result["status"], "completed", result.get("error"))
            steps = result["steps"]
            self.assertEqual(steps[0]["type"], "memory_context")
            claim, = steps[0]["claims"]
            self.assertEqual((claim["scope"], claim["revision"], claim["author"]), (scope, 2, "user"))
            self.assertEqual(claim["sources"], old["claims"][0]["sources"])
            self.assertIn("Night crew", claim["evidence_changes"][0]["scope"])
            self.assertNotIn("Annex", json.dumps(steps[0]))
            self.assertEqual([item["name"] for step in steps if step["type"] == "response"
                              for item in step["response"]["output"] if item["type"] == "function_call"],
                             ["read_cited_evidence"])
            self.assertIn("west entrance", client.get(location).text)
            self.assertIn("Original handbook", client.get(claim["note_url"]).text)

    def test_nested_names_only_activate_the_scope_of_each_independent_mention(self):
        self.seed("Meridian", "Take route A.")
        self.seed("Meridian Annex", "Take route B.")
        self.seed("Annex", "Take route C.")
        for request, scopes in (
            ("Which route at Meridian Annex?", {"Meridian Annex"}),
            ("Compare Meridian Annex with Meridian.", {"Meridian", "Meridian Annex"}),
            ("Compare Meridian and MERIDIAN ANNEX.", {"Meridian", "Meridian Annex"}),
            ("Compare Annex and Meridian Annex.", {"Annex", "Meridian Annex"}),
        ):
            with self.subTest(request=request):
                preview = self.store.relevant_project_memory(request, [])
                self.assertEqual({claim["scope"] for claim in preview["claims"]}, scopes)
                self.assertEqual(preview["candidates"], [])
        earlier = self.store.relevant_project_memory("Do that again.", ["Meridian Annex"])
        self.assertEqual(earlier["claims"], [])
        self.assertEqual([item["scope"] for item in earlier["candidates"]], ["Meridian Annex"])

    def test_content_only_match_is_a_scope_hint_and_user_edits_replace_indexed_terms(self):
        self.seed("Orchard Shed", "Draft the lantern inventory before closing.")
        note = self.store.read_project_note("Orchard Shed")
        hints = self.store.relevant_project_memory("What is required for lantern inventory?", [])
        self.assertEqual(hints["claims"], [])
        self.assertEqual([r["scope"] for r in hints["candidates"]], ["Orchard Shed"])
        self.assertNotIn("sources", hints["candidates"][0])
        self.store.edit_project_note(note["id"], 1, [{"text": "Count spare batteries.",
                                     "sources": note["claims"][0]["sources"]}], "Updated instruction.")
        self.assertEqual(self.store.relevant_project_memory("What about lantern inventory?", []), {})
        self.assertEqual(self.store.relevant_project_memory("Review sales.", []), {})
        self.assertEqual(self.store.relevant_project_memory("Do that again.", ["Orchard Shed"])["claims"], [])
        with self.store._connection() as connection:
            connection.execute("DROP TRIGGER refresh_current_note_search")
            connection.execute("DROP TABLE current_note_search")
        self.store.initialize()
        self.store.initialize()
        current = self.store.relevant_project_memory("Prepare the Orchard Shed opening.", [])
        self.assertEqual(current["claims"][0]["text"], "Count spare batteries.")
        self.assertEqual(current["claims"][0]["revision"], 2)

    def test_preview_respects_byte_limit_and_first_cited_claim_is_reopenable(self):
        self.seed("Aperture", "First condition")
        note = self.store.read_project_note("Aperture")
        self.store.edit_project_note(note["id"], 1, [
            {"text": f"Condition {i} " + "é" * 990, "sources": note["claims"][0]["sources"]} for i in range(12)
        ], "Keep this set of long conditions.")
        preview = self.store.relevant_project_memory("Review Aperture.", [])
        self.assertLessEqual(encoded_size(preview), PREVIEW_BYTES)
        self.assertGreater(len(preview["claims"]), 0)
        self.assertLess(len(preview["claims"]), 12)
        self.assertIn("note_url", preview["claims"][0])
