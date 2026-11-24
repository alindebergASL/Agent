import json
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.web import create_app


class DelayedUpdateModel:
    def __init__(self):
        self.draft_staged = Event()
        self.finish = Event()

    def respond(self, history, tools, instructions, *, max_tool_calls):
        user = next(item for item in reversed(history) if item.get("role") == "user")
        request = json.loads(user["content"])
        outputs = tool_outputs(history)
        if request["request"] == "Compare the original quote.":
            if outputs:
                return {"status": "completed", "output": [answer("Saved the original comparison.")]}
            calls = [
                call("original-note", "write_artifact", name="recommendation.md", content="# Original\n"),
                call("original-csv", "write_artifact", name="comparison.csv", content="price\n100\n"),
            ]
        elif "updated-note" in outputs:
            assert outputs["updated-note"]["staged"] == "recommendation.md"
            assert outputs["updated-csv"]["staged"] == "comparison.csv"
            self.draft_staged.set()
            if not self.finish.wait(timeout=10):
                raise RuntimeError("The test did not release the delayed result.")
            calls = [answer("Updated both files.")]
        elif "pinned-note" in outputs:
            assert outputs["pinned-note"]["revision"] == 1
            assert outputs["pinned-note"]["lines"] == [{"number": 1, "text": "# Original"}]
            calls = [
                call("updated-note", "write_artifact", name="recommendation.md", content="# Agent draft\n"),
                call("updated-csv", "write_artifact", name="comparison.csv", content="price\n200\n"),
            ]
        else:
            assert request["uploaded_files"] == ["quote.csv"]
            assert any(item.get("content") == "Compare the original quote." for item in history)
            assert any(item.get("content") == "Saved the original comparison." for item in history)
            calls = [
                call("pinned-note", "read_saved_artifact", name="recommendation.md", start_line=1, line_count=10),
                call("pinned-csv", "read_saved_artifact", name="comparison.csv", start_line=1, line_count=10),
            ]
        return {"status": "completed", "output": calls}


class ContinuityTests(unittest.TestCase):
    def wait_for_status(self, client, url, status):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = client.get(url + "/history")
            if response.json()["status"] == status:
                return response.json()
            time.sleep(0.05)
        self.fail(f"Assignment did not reach {status}. Last history: {response.text}")

    def test_late_agent_result_cannot_overwrite_newer_markdown_edit(self):
        model = DelayedUpdateModel()
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(data_dir=Path(directory), model=model)) as client:
                try:
                    submitted = client.post(
                        "/tasks", data={"request": "Compare the original quote."},
                        files={"files": ("quote.csv", b"price\n100\n", "text/csv")},
                        follow_redirects=False,
                    )
                    self.assertEqual(submitted.status_code, 303)
                    assignment = submitted.headers["location"]
                    self.wait_for_status(client, assignment, "completed")
                    markdown = assignment + "/artifacts/recommendation.md"
                    csv = assignment + "/artifacts/comparison.csv"
                    follow_up = client.post(
                        assignment + "/follow-up", data={"request": "Update the comparison."},
                        follow_redirects=False,
                    )
                    self.assertEqual(follow_up.status_code, 303, follow_up.text)
                    self.assertTrue(model.draft_staged.wait(timeout=5))

                    user_content = "# Original\n\nConfirm receiving hours before placing the order.\n"
                    edited = client.post(
                        markdown, data={"expected_revision": "1", "content": user_content},
                        follow_redirects=False,
                    )
                    self.assertEqual(edited.status_code, 303, edited.text)
                    model.finish.set()

                    history = self.wait_for_status(client, assignment, "failed")
                    self.assertIn("edited", history["error"])
                    self.assertEqual(client.get(markdown, params={"download": 1}).text, user_content)
                    self.assertEqual(client.get(markdown, params={"download": 1, "revision": 1}).text, "# Original\n")
                    self.assertEqual(client.get(csv, params={"download": 1}).text, "price\n100\n")
                finally:
                    model.finish.set()
