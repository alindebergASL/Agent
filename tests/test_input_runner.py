import json
import unittest
from unittest.mock import Mock

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import MAX_QUESTION_LENGTH, MAX_TOOL_CALLS, AssignmentError, NeedsUserInput, run_assignment


def response(*items):
    return {"status": "completed", "output": list(items)}


class InputRunnerTests(unittest.TestCase):
    def test_valid_question_is_recorded_and_ends_model_loop(self):
        ask = call("ask", "request_user_input", question="  Which warehouse should receive the order?  ")
        model = Mock()
        model.respond.return_value = response(ask, answer("Done"))
        records = []
        with self.assertRaises(NeedsUserInput) as paused:
            run_assignment(model, "Prepare the order.", {}, Mock(), Mock(), records.append)

        self.assertEqual(paused.exception.question, "Which warehouse should receive the order?")
        self.assertEqual(tool_outputs(records)["ask"], {"question": paused.exception.question})
        model.respond.assert_called_once()
        tools = model.respond.call_args.args[1]
        question_tool = next(tool for tool in tools if tool.get("name") == "request_user_input")
        self.assertNotIn("programmatic", question_tool.get("allowed_callers", []))

    def test_mixed_batch_has_no_effect_and_can_retry_as_single_question(self):
        ask = call("ask", "request_user_input", question="What is the deadline?")
        early_write = call("early", "write_artifact", name="early.md", content="before")
        late_write = call("late", "write_artifact", name="late.md", content="after")
        model = Mock()
        model.respond.side_effect = [
            response(early_write, ask, late_write),
            response(call("retry", "request_user_input", question="What is the deadline?")),
        ]
        stage = Mock()
        records = []
        with self.assertRaises(NeedsUserInput):
            run_assignment(model, "Draft the timeline.", {}, stage, Mock(), records.append)

        stage.assert_not_called()
        outputs = tool_outputs(records)
        self.assertTrue(all("error" in outputs[call_id] for call_id in ("early", "ask", "late")))
        self.assertEqual(outputs["retry"], {"question": "What is the deadline?"})
        self.assertEqual(model.respond.call_args.kwargs["max_tool_calls"], MAX_TOOL_CALLS - 3)

    def test_invalid_and_programmatic_questions_return_errors_to_model(self):
        invalid = [
            call("blank", "request_user_input", question="  "),
            call("object", "request_user_input", question={"ask": "Which audience?"}),
            call("long", "request_user_input", question="x" * (MAX_QUESTION_LENGTH + 1)),
            call("extra", "request_user_input", question="Which audience?", urgency="high"),
            {**call("bad-json", "request_user_input"), "arguments": "not-json"},
            {**call("hosted", "request_user_input", question="Which audience?"),
             "caller": {"type": "program", "caller_id": "p"}},
        ]
        model = Mock()
        model.respond.side_effect = [*(response(item) for item in invalid), response(answer("Completed."))]
        records = []
        final = run_assignment(model, "Write an overview.", {}, Mock(), Mock(), records.append)

        self.assertEqual(final, "Completed.")
        outputs = tool_outputs(records)
        for item in invalid:
            self.assertIn("error", outputs[item["call_id"]])

    def test_question_cannot_exceed_tool_budget(self):
        reads = [call(f"calc-{n}", "calculate", expression="1") for n in range(MAX_TOOL_CALLS)]
        model = Mock()
        model.respond.side_effect = [response(*reads), response(call("ask", "request_user_input", question="Where?"))]
        records = []
        with self.assertRaisesRegex(AssignmentError, "tool call limit"):
            run_assignment(model, "Prepare an estimate.", {}, Mock(), Mock(), records.append)
        self.assertNotIn("ask", tool_outputs(records))

    def test_resumed_turn_receives_original_request_question_answer_and_uploads(self):
        original = "Compare the two proposals."
        question = "Which teams submitted proposals?"
        reply = "A and B. The second proposal is attached."
        model = Mock()
        model.respond.return_value = response(answer("Comparison complete."))
        result = run_assignment(
            model, reply, {"A.csv": "a", "B.csv": "b"}, Mock(), Mock(), Mock(),
            prior_messages=[{"role": "user", "content": original}, {"role": "assistant", "content": question}],
        )
        self.assertEqual(result, "Comparison complete.")
        # A final assistant message has also been appended to the history after respond.
        first, second, third = model.respond.call_args.args[0][:3]
        self.assertEqual((first["role"], first["content"]), ("user", original))
        self.assertEqual((second["role"], second["content"]), ("assistant", question))
        self.assertEqual((third["role"], json.loads(third["content"])), (
            "user", {"request": reply, "uploaded_files": ["A.csv", "B.csv"]},
        ))
