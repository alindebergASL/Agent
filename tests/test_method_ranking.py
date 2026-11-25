import json
import tempfile
import unittest
from pathlib import Path

from tests.method_policy_eval import evaluate, score
from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import run_assignment
from work_agent.storage import Store


class MethodRankingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name))
        self.store.initialize()
        self.source = self.store.submit("Curate methods", {})
        self.assertEqual(self.store.claim(), self.source)
        self.store.complete(self.source, "Done.")

    def save(self, title, scope):
        return self.store.save_method(self.source, title, scope, "Public procedure", "Private evidence")

    def test_existing_methods_backfill_and_follow_edits_without_searching_evidence(self):
        target = self.save("Review a renewal", "Applies to licenses at renewal.")
        bolts = self.save("Count bolts", "Inventory counting.")
        with self.store._connection() as connection:
            for suffix in ("add", "edit", "remove"):
                connection.execute(f"DROP TRIGGER method_search_{suffix}")
            connection.execute("DROP TABLE method_search")
        self.store.initialize()
        self.assertEqual(self.store.relevant_methods("What about renewing licenses?", [])[0]["id"], target)
        self.assertEqual(self.store.relevant_methods("Yes please", ["renewals"])[0]["id"], target)
        self.assertEqual([m["id"] for m in self.store.relevant_methods("Count bolts", ["renewals"])], [bolts, target])
        self.assertEqual(self.store.relevant_methods("private evidence", []), [])
        self.assertEqual(self.store.relevant_methods('" OR *', []), [])
        self.store.update_method(target, "Archive a patent", "Expired inventions.", "File it.", "Renewal",
                                 expected_revision=1)
        self.assertEqual(self.store.relevant_methods("renewals", []), [])
        self.assertEqual(self.store.relevant_methods("inventions", [])[0]["id"], target)
        self.store.remove_method(target, expected_revision=2)
        self.assertEqual(self.store.relevant_methods("inventions", []), [])

    def test_method_outside_first_page_can_be_read_and_unrelated_scopes_are_omitted(self):
        scope = "Inventory checks. " + "Verify the original invoice before resale. " * 9
        for index in range(36):
            target = self.save(f"Inventory class {index:02d}", scope)
        unrelated = self.save("Compile a bibliography", "Cite book editions.")
        offers = self.store.relevant_methods("Process inventory", [])
        self.assertEqual(len(offers), 36)
        self.assertNotIn(unrelated, [row["id"] for row in offers])

        class PageReader:
            def respond(inner, history, tools, instructions, *, max_tool_calls):
                request = json.loads(history[0]["content"])
                results = tool_outputs(history)
                self.assertNotIn(target, [row["id"] for row in request["saved_methods"]])
                if "attempt" not in results:
                    output = call("attempt", "read_method", method_id=target)
                else:
                    self.assertIn("error", results["attempt"])
                    page = request
                    for name, result in results.items():
                        if name.startswith("page-"):
                            page = result
                    if "read" in results:
                        self.assertEqual(results["read"]["procedure"], "Public procedure")
                        output = answer("Used the appropriate inventory method.")
                    elif target in [row["id"] for row in page["saved_methods"]]:
                        output = call("read", "read_method", method_id=target)
                    else:
                        offset = page["next_method_offset"]
                        output = call(f"page-{offset}", "list_more_methods", offset=offset)
                return {"status": "completed", "output": [output]}

        self.assertEqual(run_assignment(
            PageReader(), "Process inventory", {}, lambda *_: None, lambda *_: None, lambda *_: None,
            method_index=offers, read_method=self.store.get_method,
        ), "Used the appropriate inventory method.")

    def test_paired_evaluator_uses_actual_application_outcomes(self):
        for case in ("split", "drill_failed"):
            with self.subTest(case=case):
                for arm, expected_read in (("recent-eight", False), ("relevance", True)):
                    row = evaluate(case, arm, "fixture")
                    self.assertEqual(row["status"], "completed", row)
                    self.assertTrue(row["decision_pass"], row)
                    self.assertEqual(row["target_method_read"], expected_read)
        wrong = "decision,next_action,rule_source,evidence_source\nship,deploy,release.md:2,release.md:4\n"
        self.assertFalse(any(score("drill_failed", wrong).values()))
        self.assertEqual(score("split", ""), {"totals": False, "winner": False, "citations": False})


if __name__ == "__main__":
    unittest.main()
