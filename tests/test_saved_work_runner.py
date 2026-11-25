import unittest
from unittest.mock import Mock

from tests.quote_fixture import answer, call, tool_outputs
from tests.test_prior_runner import HOSTED, read, response
from work_agent.runner import run_assignment


class SavedWorkRunnerTests(unittest.TestCase):
    def run_search(self, model_items):
        source = {"source_task_id": "old-task", "name": "purchases.csv", "revision": 4}
        description = {"request": "Prepare a purchases CSV", "created_at": "2026-11-25T14:00:00Z"}
        search = Mock(return_value=[{**source, **description}])
        pin = Mock(return_value={**source, "author": "user", "reference_id": 8})
        reader = Mock(return_value=b"item,units\napples,20\n")
        model = Mock()
        model.respond.side_effect = [*model_items, response(answer("Done."))]
        trace = []
        run_assignment(model, "Find my purchases file", {}, Mock(), Mock(), trace.append,
                       search_saved_work=search, pin_saved_work=pin, read_prior_artifact=reader)
        return model, trace, search, pin, reader

    def test_search_returns_metadata_and_open_pins_the_displayed_revision(self):
        model, trace, search, pin, reader = self.run_search([
            response(call("find", "search_saved_work", query="purchases")),
            response({**call("again", "search_saved_work", query="purchases.csv"), "caller": HOSTED}),
            response({**call("open", "open_saved_work", candidate_id=1), "caller": HOSTED}),
            response(read("lines", reference_id=8)),
        ])
        results = tool_outputs(trace)
        candidates = results["find"]["candidates"]
        self.assertEqual(candidates, [{"candidate_id": 1, "name": "purchases.csv", "revision": 4,
                                      "request": "Prepare a purchases CSV", "created_at": "2026-11-25T14:00:00Z"}])
        self.assertEqual(results["again"]["candidates"], candidates)
        self.assertEqual([c.args for c in search.call_args_list], [("purchases",), ("purchases.csv",)])
        pin.assert_called_once_with("old-task", "purchases.csv", 4)
        reader.assert_called_once_with(8)
        self.assertEqual(results["open"]["revision_url"], "../prior-artifacts/8")
        self.assertEqual(results["lines"]["lines"][1], {"number": 2, "text": "apples,20"})
        self.assertEqual(results["lines"]["revision_url"], results["open"]["revision_url"])
        tools = {tool.get("name"): tool for tool in model.respond.call_args.args[1]}
        for name in ("search_saved_work", "open_saved_work", "read_prior_artifact"):
            self.assertEqual(tools[name]["allowed_callers"], ["direct", "programmatic"])

    def test_only_previously_delivered_candidates_and_references_can_be_opened(self):
        _, trace, _, pin, reader = self.run_search([
            response(call("find", "search_saved_work", query="purchases"),
                     call("premature", "open_saved_work", candidate_id=1)),
            response(call("bool", "open_saved_work", candidate_id=True),
                     call("absent", "open_saved_work", candidate_id=2),
                     read("before_open", reference_id=8)),
            response(call("valid", "open_saved_work", candidate_id=1), read("same_batch", reference_id=8)),
        ])
        results = tool_outputs(trace)
        for key in ("premature", "bool", "absent", "before_open", "same_batch"):
            self.assertIn("error", results[key])
        pin.assert_called_once_with("old-task", "purchases.csv", 4)
        reader.assert_not_called()
