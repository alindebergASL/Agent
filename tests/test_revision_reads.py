import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import run_assignment
from work_agent.storage import Store


class RevisionReadTests(unittest.TestCase):
    def test_one_page_full_read_does_not_authorize_write_in_same_response(self):
        name = "report.md"
        original = "User note: confirm receiving hours.\n"
        updated = original + "Updated forecast: 12 units.\n"
        model = Mock()
        model.respond.side_effect = [
            {"status": "completed", "output": [
                call("read", "read_saved_artifact", name=name, start_line=1, line_count=10),
                call("premature", "write_artifact", name=name, content="Replacement without the note.\n"),
            ]},
            {"status": "completed", "output": [
                call("replacement", "write_artifact", name=name, content=updated),
            ]},
            {"status": "completed", "output": [answer("Saved the updated report.")]},
        ]
        stage = Mock()
        steps = []

        response = run_assignment(
            model, "Add the updated forecast.", {}, stage, Mock(), steps.append,
            saved_artifacts={name: {"revision": 1, "author": "user"}},
            read_saved_artifact=Mock(return_value=original.encode()),
        )

        outputs = tool_outputs(steps)
        self.assertEqual(outputs["read"]["total_lines"], 1)
        self.assertEqual(outputs["read"]["lines"], [{"number": 1, "text": original.strip()}])
        self.assertIn("saved revision", outputs["premature"]["error"])
        self.assertEqual(outputs["replacement"]["staged"], name)
        stage.assert_called_once_with(name, updated)
        self.assertEqual(response, "Saved the updated report.")

    def test_replacement_requires_full_revision_in_prior_tool_receipts(self):
        name = "report.md"
        note = "User note: confirm receiving hours before placing the order."
        lines = ["# Report", "", *(f"Finding {index}" for index in range(1, 104)), note]
        user_content = "\n".join(lines) + "\n"
        addition = "\nUpdated forecast: 12 units.\n"
        self.assertGreater(len(lines), 100)

        directory = self.enterContext(tempfile.TemporaryDirectory())
        store = Store(Path(directory))
        store.initialize()
        task_id = store.submit("Make a report.", {})
        self.assertEqual(store.claim(), task_id)
        first = store.get_run(task_id)["turn_id"]
        store.stage(task_id, name, "# Report\n", turn_id=first)
        store.complete(task_id, "Report saved.", turn_id=first)
        self.assertEqual(store.save_markdown(task_id, name, user_content, 1), 2)
        turn_id = store.follow_up(task_id, "Add the updated forecast to the report.")
        self.assertEqual(store.claim(), task_id)
        run = store.get_run(task_id)

        def read(call_id, start_line, line_count):
            return call(call_id, "read_saved_artifact", name=name, start_line=start_line, line_count=line_count)

        def write(call_id, content):
            return call(call_id, "write_artifact", name=name, content=content)

        def respond(history, tools, instructions, *, max_tool_calls):
            outputs = tool_outputs(history)
            if "write-complete" in outputs:
                messages = [answer("Saved the updated report.")]
            elif "read-tail" in outputs:
                received_lines = {
                    line["number"]: line["text"]
                    for key in ("read-head", "read-overlap", "read-tail")
                    for line in outputs[key]["lines"]
                }
                text = "\n".join(received_lines[index] for index in sorted(received_lines)) + "\n"
                messages = [write("write-complete", text + addition)]
            elif "read-head" in outputs:
                messages = [
                    write("write-partial", "Partial replacement.\n"),
                    read("read-tail", 101, 100),
                    write("write-same-response", "Replacement before seeing the last page.\n"),
                ]
            else:
                # The duplicate lines have to be counted once, even across pages.
                messages = [read("read-head", 1, 100), read("read-overlap", 95, 6)]
            return {"status": "completed", "output": messages}

        model = Mock()
        model.respond.side_effect = respond
        stage = Mock(side_effect=lambda artifact, content: store.stage(task_id, artifact, content, turn_id=turn_id))
        response = run_assignment(
            model,
            run["request"],
            store.read_uploads(task_id),
            stage,
            lambda message: store.progress(task_id, message, turn_id=turn_id),
            lambda step: store.record_step(task_id, step, turn_id=turn_id),
            prior_messages=run["prior_messages"],
            saved_artifacts=run["artifacts"],
            read_saved_artifact=lambda artifact: store.read_run_artifact(task_id, turn_id, artifact),
        )
        store.complete(task_id, response, turn_id=turn_id)

        outputs = tool_outputs(store.run_history(task_id)["turns"][-1]["steps"])
        self.assertEqual(outputs["read-head"]["revision"], 2)
        self.assertEqual(outputs["read-head"]["total_lines"], len(lines))
        self.assertEqual(len(outputs["read-head"]["lines"]), 100)
        self.assertEqual(outputs["read-tail"]["lines"][-1], {"number": len(lines), "text": note})
        self.assertIn("saved revision", outputs["write-partial"]["error"])
        self.assertIn("saved revision", outputs["write-same-response"]["error"])
        self.assertEqual(outputs["write-complete"]["staged"], name)
        stage.assert_called_once_with(name, user_content + addition)
        self.assertEqual(store.read_artifact(task_id, name), (user_content + addition).encode())
