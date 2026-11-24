import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from tests.quote_fixture import answer, call
from work_agent.runner import AssignmentError, OpenAIModel, RESPONSES_TOOLS, run_assignment


def completed(*items):
    return {"status": "completed", "output": list(items)}


def search(index):
    return {
        "type": "web_search_call",
        "id": f"ws_{index}",
        "status": "completed",
        "action": {"type": "search", "queries": ["example resources"]},
    }


class RunnerReviewTests(unittest.TestCase):
    def test_commentary_and_reasoning_turns_continue(self):
        commentary = {**answer("I will check the details."), "phase": "commentary"}
        reasoning = {"type": "reasoning", "id": "rs_fixture", "encrypted_content": "opaque", "summary": []}
        responses = iter((completed(commentary), completed(reasoning), completed(answer("Here is the result."))))
        histories = []

        def respond(history, tools, instructions, *, max_tool_calls):
            histories.append(deepcopy(history))
            return next(responses)

        model = Mock()
        model.respond.side_effect = respond

        result = run_assignment(model, "Check the details.", {}, Mock(), Mock(), Mock())

        self.assertEqual(result, "Here is the result.")
        self.assertEqual(len(histories), 3)
        self.assertEqual(histories[1], [*histories[0], commentary])
        self.assertEqual(histories[2], [*histories[1], reasoning])

    def test_refusal_is_terminal_even_with_web_search(self):
        refusal = "I cannot complete this request."
        message = {
            **answer(""),
            "phase": "final_answer",
            "content": [{"type": "refusal", "refusal": refusal}],
        }
        for earlier_output in ((), (search(1),)):
            with self.subTest(with_web_search=bool(earlier_output)):
                model = Mock()
                model.respond.side_effect = [
                    completed(*earlier_output, message),
                    completed(answer("Tried again.")),
                ]
                stage = Mock()

                with self.assertRaises(AssignmentError) as raised:
                    run_assignment(model, "Research the topic.", {}, stage, Mock(), Mock())

                self.assertIn(refusal, str(raised.exception))
                model.respond.assert_called_once()
                stage.assert_not_called()

    def test_hosted_events_and_function_calls_share_remaining_budget(self):
        stage = Mock()
        record = Mock()
        write = call("write", "write_artifact", name="brief.md", content="# Brief\n")
        model = Mock()
        model.respond.side_effect = [
            completed(*(search(index) for index in range(47))),
            completed(write, search(47)),
        ]

        with self.assertRaisesRegex(AssignmentError, r"(?i)tool[ -]call.*(?:limit|budget)"):
            run_assignment(model, "Research and save a brief.", {}, stage, Mock(), record)

        self.assertEqual([item.kwargs["max_tool_calls"] for item in model.respond.call_args_list], [48, 1])
        stage.assert_not_called()
        self.assertFalse(any(item.args[0]["type"] == "function_call_output" for item in record.call_args_list))

    def test_final_text_can_follow_exact_tool_budget(self):
        content = "# Brief\n"
        write = call("write", "write_artifact", name="brief.md", content=content)
        model = Mock()
        model.respond.side_effect = [
            completed(*(search(index) for index in range(47)), write),
            completed(answer("Saved the brief.")),
        ]
        stage = Mock()

        result = run_assignment(model, "Research and save a brief.", {}, stage, Mock(), Mock())

        self.assertEqual(result, "Saved the brief.")
        self.assertEqual([item.kwargs["max_tool_calls"] for item in model.respond.call_args_list], [48, 0])
        stage.assert_called_once_with("brief.md", content)

    def test_provider_envelope_respects_budget_and_preserves_tools(self):
        client = Mock()
        with patch("work_agent.runner.OpenAI", return_value=client):
            model = OpenAIModel("fixture", "gpt-6.2-sol")
        history = [{"type": "message", "role": "user", "content": "Research the topic."}]

        model.respond(history, RESPONSES_TOOLS, "Use research when needed.", max_tool_calls=7)

        request = client.responses.create.call_args.kwargs
        self.assertEqual(request["max_tool_calls"], 7)
        self.assertNotEqual(request.get("tool_choice"), "none")
        self.assertEqual(request["tools"], RESPONSES_TOOLS)

        model.respond(history, RESPONSES_TOOLS, "Use research when needed.", max_tool_calls=0)

        request = client.responses.create.call_args.kwargs
        self.assertEqual(request["tool_choice"], "none")
        self.assertNotIn("max_tool_calls", request)
        self.assertEqual(request["tools"], RESPONSES_TOOLS)
        self.assertEqual(request["input"], history)
