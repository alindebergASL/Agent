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
        self.assertEqual(candidates, [{"candidate_id": 1, "kind": "artifact", "name": "purchases.csv", "revision": 4,
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

    def test_original_and_artifact_with_same_name_are_separate_readable_candidates(self):
        metadata = {"name": "quote.csv", "source_task_id": "quotes", "request": "Compare quotes",
                    "created_at": "2026-11-25T14:00:00Z"}
        source = {**metadata, "kind": "upload", "revision": None}
        search = Mock(return_value=[{**metadata, "kind": "artifact", "revision": 1}, source])
        pin = Mock(return_value={**source, "reference_id": 9})
        reader = Mock(return_value=b"item,quoted_price\r\n000731,13.50\r\n")
        model = Mock()
        model.respond.side_effect = [
            response(call("search", "search_saved_work", query="quote.csv")),
            response(call("source", "open_saved_work", candidate_id=2)),
            response({**read("row", reference_id=9, start_line=2, line_count=1), "caller": HOSTED}),
            response(answer("Read the original.")),
        ]
        trace = []
        run_assignment(model, "Find the original quote", {}, Mock(), Mock(), trace.append,
                       search_saved_work=search, pin_saved_work=pin, read_prior_artifact=reader)
        result = tool_outputs(trace)
        self.assertEqual([(c["candidate_id"], c["kind"]) for c in result["search"]["candidates"]],
                         [(1, "artifact"), (2, "upload")])
        pin.assert_called_once_with("quotes", "quote.csv", None)
        reader.assert_called_once_with(9)
        self.assertEqual(result["row"]["kind"], "upload")
        self.assertEqual(result["row"]["revision_url"], "../prior-artifacts/9")
        self.assertEqual(result["row"]["lines"], [{"number": 2, "text": "000731,13.50"}])

    def test_kind_filter_reaches_search_before_candidates_are_ranked(self):
        _, trace, search, _, _ = self.run_search([
            response(call("either", "search_saved_work", query="quote", kind="any"),
                     call("original", "search_saved_work", query="quote", kind="upload"),
                     call("generated", "search_saved_work", query="quote", kind="artifact"),
                     call("invalid", "search_saved_work", query="quote", kind="unknown")),
        ])
        self.assertEqual([c.args for c in search.call_args_list],
                         [("quote",), ("quote", "upload"), ("quote", "artifact")])
        self.assertIn("error", tool_outputs(trace)["invalid"])

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
