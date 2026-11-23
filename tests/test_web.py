import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import AssignmentError
from work_agent.storage import Store
from work_agent.web import create_app


class InventoryModel:
    """Exercise the same tools with a different input, request, and output."""

    def respond(self, input, tools, instructions):
        outputs = tool_outputs(input)
        if "write" in outputs:
            if outputs["write"] != {"staged": "inventory.md"}:
                raise ValueError("The inventory summary was not staged.")
            output = [answer("Saved the inventory summary.")]
        elif "count" in outputs:
            count = outputs["count"]["result"]
            output = [call(
                "write", "write_artifact", name="inventory.md",
                content=f"# Inventory\n\n{count} units in stock. Source: batches.txt:1–2.\n",
            )]
        elif "read" in outputs:
            cartons, units = [line["text"] for line in outputs["read"]["lines"]]
            output = [call("count", "calculate", expression=f"{cartons} * {units}")]
        else:
            user = next(item for item in input if item.get("role") == "user")
            file, = json.loads(user["content"])["uploaded_files"]
            output = [call("read", "read_file", file_id=file, start_line=1, line_count=2)]
        return {"status": "completed", "output": output}


class FailingModel:
    def respond(self, input, tools, instructions):
        outputs = tool_outputs(input)
        if outputs:
            if outputs["write"] != {"staged": "partial.md"}:
                raise ValueError("The draft was not staged.")
            raise AssignmentError("The fixture stopped before publication.")
        return {"status": "completed", "output": [call(
            "write", "write_artifact", name="partial.md", content="Unpublished draft.",
        )]}


class WebTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.data_dir = Path(folder)

    def client(self, model):
        return TestClient(create_app(data_dir=self.data_dir, model=model))

    def submit(self, client, contents=b"3\r\n7\r\n"):
        response = client.post(
            "/tasks",
            data={"request": "Calculate the total stock from the attached counts and save a summary."},
            files=[("files", ("batches.txt", contents, "text/plain"))],
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        return response.headers["location"]

    def wait_for_task(self, client, location, status):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            page = client.get(location)
            self.assertEqual(page.status_code, 200)
            if f'data-task-status="{status}"' in page.text:
                return page.text
            time.sleep(0.05)
        self.fail(f"Assignment at {location} did not reach {status}: {page.text}")

    def test_artifact_reopens_after_app_restart(self):
        with self.client(InventoryModel()) as client:
            location = self.submit(client)
            self.assertIn("Saved the inventory summary.", self.wait_for_task(client, location, "completed"))
            download_path = f"{location}/artifacts/inventory.md?download=1"
            saved = client.get(download_path)
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(
                saved.content, "# Inventory\n\n21 units in stock. Source: batches.txt:1–2.\n".encode(),
            )

        task_id = location.rsplit("/", 1)[1]
        self.assertEqual(Store(self.data_dir).read_upload(task_id, "batches.txt"), b"3\r\n7\r\n")

        with self.client(InventoryModel()) as client:
            index = client.get("/")
            self.assertIn(location, index.text)
            result = client.get(download_path)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.content, saved.content)
            preview = client.get(f"{location}/artifacts/inventory.md")
            self.assertEqual(preview.status_code, 200)
            self.assertIn(location, preview.text)
            self.assertIn("21 units in stock.", preview.text)

    def test_failed_assignment_does_not_publish_staged_artifact(self):
        with self.client(FailingModel()) as client:
            location = self.submit(client)
            page = self.wait_for_task(client, location, "failed")
            self.assertIn("The fixture stopped before publication.", page)
            self.assertNotIn("partial.md", page)
            artifact = client.get(f"{location}/artifacts/partial.md?download=1")
            self.assertEqual(artifact.status_code, 404)

    def test_binary_upload_is_rejected(self):
        with self.client(InventoryModel()) as client:
            response = client.post(
                "/tasks",
                data={"request": "Read this file"},
                files=[("files", ("batches.txt", b"\xff\xfe\x00", "text/plain"))],
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(Store(self.data_dir).list_tasks(), [])


if __name__ == "__main__":
    unittest.main()
