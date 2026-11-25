import json
import unittest
from unittest.mock import Mock

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import run_assignment


NAME = "weekly.md"
PRIOR = {"reference_id": 7, "name": NAME, "revision": 3, "source_task_id": "prior-task", "author": "user"}
HOSTED = {"type": "program", "caller_id": "compare"}


def response(*items):
    return {"status": "completed", "output": list(items)}


def read(call_id, reference_id=7, start_line=1, line_count=100):
    return call(call_id, "read_prior_artifact", reference_id=reference_id, start_line=start_line, line_count=line_count)


class PriorRunnerTests(unittest.TestCase):
    def test_exact_prior_revision_is_paged_with_attribution_for_direct_and_hosted_calls(self):
        model = Mock()
        model.respond.side_effect = [
            response(read("first"), {**read("second", start_line=101), "caller": HOSTED}),
            response(answer("Reviewed the earlier brief.")),
        ]
        contents = "\n".join(f"Observation {i}" for i in range(1, 102)).encode()
        source = Mock(return_value=contents)
        records = []
        run_assignment(model, "Report material changes.", {}, Mock(), Mock(), records.append,
                       prior_artifacts=[PRIOR], read_prior_artifact=source)
        history, tools, instructions = model.respond.call_args.args
        reference = {**PRIOR, "revision_url": "../prior-artifacts/7"}
        self.assertEqual(json.loads(history[0]["content"])["prior_artifacts"], [reference])
        tool = next(item for item in tools if item.get("name") == "read_prior_artifact")
        self.assertEqual(tool["allowed_callers"], ["direct", "programmatic"])
        self.assertIn("relative links", instructions.lower())
        outputs = tool_outputs(records)
        for key in ("first", "second"):
            self.assertEqual({field: outputs[key][field] for field in reference}, reference)
            self.assertEqual(outputs[key]["total_lines"], 101)
        self.assertEqual(len(outputs["first"]["lines"]), 100)
        self.assertEqual(outputs["second"]["lines"], [{"number": 101, "text": "Observation 101"}])
        receipts = [item for item in records if item["type"] == "function_call_output"]
        self.assertEqual(receipts[-1]["caller"], HOSTED)
        self.assertEqual([c.args for c in source.call_args_list], [(7,), (7,)])

    def test_prior_read_cannot_authorize_replacing_same_named_current_artifact(self):
        model = Mock()
        model.respond.side_effect = [
            response(read("prior")),
            response(call("premature", "write_artifact", name=NAME, content="overwrite")),
            response(call("current", "read_saved_artifact", name=NAME, start_line=1, line_count=100)),
            response(call("updated", "write_artifact", name=NAME, content="User note. New evidence.")),
            response(answer("Saved.")),
        ]
        records = []
        stage = Mock()
        run_assignment(model, "Update this week's brief.", {}, stage, Mock(), records.append,
                       prior_artifacts=[PRIOR], read_prior_artifact=Mock(return_value=b"Last week."),
                       saved_artifacts={NAME: {"revision": 1, "author": "user"}},
                       read_saved_artifact=Mock(return_value=b"User note."))
        outputs = tool_outputs(records)
        self.assertIn("read_saved_artifact", outputs["premature"]["error"])
        self.assertEqual(outputs["current"]["lines"], [{"number": 1, "text": "User note."}])
        stage.assert_called_once_with(NAME, "User note. New evidence.")

    def test_only_listed_reference_ids_reach_reader_and_unavailable_content_is_reported(self):
        model = Mock()
        model.respond.side_effect = [
            response(read("unlisted", 8), read("invalid_type", True), read("object", {}), read("missing")),
            response(answer("The earlier brief could not be read.")),
        ]
        source = Mock(return_value=None)
        records = []
        run_assignment(model, "Prepare a brief.", {}, Mock(), Mock(), records.append,
                       prior_artifacts=[PRIOR], read_prior_artifact=source)
        source.assert_called_once_with(7)
        outputs = tool_outputs(records)
        for key in ("unlisted", "invalid_type", "object"):
            self.assertIn("prior_artifacts", outputs[key]["error"])
        self.assertIn("unavailable", outputs["missing"]["error"])

    def test_first_or_unrelated_assignment_has_no_prior_context_or_tool(self):
        model = Mock()
        model.respond.side_effect = [response(read("guessed")), response(answer("Ready."))]
        source = Mock()
        records = []
        run_assignment(model, "Prepare a brief.", {}, Mock(), Mock(), records.append, read_prior_artifact=source)
        history, tools, _ = model.respond.call_args.args
        self.assertNotIn("prior_artifacts", json.loads(history[0]["content"]))
        self.assertNotIn("read_prior_artifact", [tool.get("name") for tool in tools])
        self.assertIn("error", tool_outputs(records)["guessed"])
        source.assert_not_called()
