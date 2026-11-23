import json
import unittest
from copy import deepcopy
from unittest.mock import Mock

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import run_assignment


class ProgrammaticTests(unittest.TestCase):
    def test_hosted_reads_replay_then_direct_artifact(self):
        program = {
            "type": "program",
            "id": "prog_fixture",
            "call_id": "batch",
            "code": "await Promise.all([read_file(north), read_file(south)]);",
            "fingerprint": "opaque-replay-data",
        }
        caller = {"type": "program", "caller_id": "batch"}
        read_calls = [
            {
                **call(name, "read_file", file_id=f"{name}.txt", start_line=1, line_count=2),
                "async": True,
                "caller": caller,
            }
            for name in ("north", "south")
        ]
        program_output = {
            "type": "program_output",
            "id": "progout_fixture",
            "call_id": "batch",
            "result": "Both sources read.",
            "status": "completed",
        }
        artifact = "# Stock\n\nNorth has 2 units and south has 3.\n"
        write_call = {
            **call("write", "write_artifact", name="stock.md", content=artifact),
            "caller": {"type": "direct"},
        }
        responses = iter((
            [program, *read_calls],
            [program_output],
            [write_call],
            [answer("Saved the stock summary.")],
        ))
        histories = []

        def respond(history, tools, instructions):
            histories.append(deepcopy(history))
            self.assertIn({"type": "programmatic_tool_calling"}, tools)
            read_tool = next(tool for tool in tools if tool.get("name") == "read_file")
            self.assertEqual(read_tool["allowed_callers"], ["direct", "programmatic"])
            return {"status": "completed", "output": next(responses)}

        model = Mock()
        model.respond.side_effect = respond
        stage = Mock()
        activity = []
        records = []
        final = run_assignment(
            model,
            "Read both files and save the stock summary.",
            {"north.txt": "units\n2\n", "south.txt": "units\n3\n"},
            stage,
            activity.append,
            records.append,
        )

        self.assertEqual(final, "Saved the stock summary.")
        stage.assert_called_once_with("stock.md", artifact)
        self.assertEqual(len(histories), 4)
        self.assertEqual(histories[1][1:4], [program, *read_calls])
        for receipt, name, number in zip(histories[1][4:], ("north", "south"), ("2", "3"), strict=True):
            self.assertEqual(receipt["type"], "function_call_output")
            self.assertEqual(receipt["call_id"], name)
            self.assertEqual(receipt["caller"], caller)
            self.assertEqual(json.loads(receipt["output"]), {
                "file_id": f"{name}.txt",
                "lines": [{"number": 1, "text": "units"}, {"number": 2, "text": number}],
                "total_lines": 2,
            })
        self.assertEqual(histories[2], [*histories[1], program_output])
        self.assertEqual(tool_outputs(histories[3])["write"]["staged"], "stock.md")
        self.assertEqual(histories[3][-1]["caller"], {"type": "direct"})
        self.assertIn("Hosted program batch called read_file (north.txt, lines 1–2).", activity)
        self.assertIn("Hosted program batch called read_file (south.txt, lines 1–2).", activity)
        for receipt in histories[1][4:]:
            self.assertIn(receipt, records)

    def test_program_cannot_stage_artifact(self):
        caller = {"type": "program", "caller_id": "batch"}
        attempt = {
            **call("write", "write_artifact", name="stock.md", content="# Stock\n"),
            "caller": caller,
        }
        model = Mock()
        model.respond.side_effect = [
            {"status": "completed", "output": [attempt]},
            {"status": "completed", "output": [answer("Could not save inside the program.")]},
        ]
        stage = Mock()
        records = []

        run_assignment(model, "Save a note.", {}, stage, Mock(), records.append)

        stage.assert_not_called()
        receipt, = (item for item in records if item["type"] == "function_call_output")
        self.assertEqual(receipt["caller"], caller)
        self.assertIn("directly", json.loads(receipt["output"])["error"])
