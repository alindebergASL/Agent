import csv
import io
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tests.quote_fixture import tool_outputs
from tests.recovery_fixture import (
    CORRECTED_ARTIFACT,
    CORRECTION_REQUEST,
    FRESH_QUOTES,
    FRESH_REQUEST,
    LOSSY_UPLOAD_NAME,
    MARKER_REQUEST,
    METHOD_PROCEDURE,
    METHOD_SCOPE,
    METHOD_TITLE,
    ORIGINAL_QUOTES,
    RECOVERY_REQUEST,
    FreshQuoteModel,
    RecoveryModel,
    lossy_import,
    quote_rows,
)
from work_agent.storage import Store
from work_agent.web import create_app


class RecoveryMethodTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.data = Path(directory)
        self.source, = ORIGINAL_QUOTES
        self.inputs = {**ORIGINAL_QUOTES, LOSSY_UPLOAD_NAME: lossy_import(ORIGINAL_QUOTES[self.source])}

    def submit(self, client, request, uploads=None):
        files = [("files", (name, content, "text/csv")) for name, content in (uploads or {}).items()]
        result = client.post("/tasks", data={"request": request}, files=files, follow_redirects=False)
        self.assertEqual(result.status_code, 303, result.text)
        return result.headers["location"]

    def history(self, client, assignment):
        result = client.get(assignment + "/history")
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def wait_completed(self, client, assignment):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            history = self.history(client, assignment)
            if history["status"] == "completed":
                return history
            if history["status"] == "failed":
                self.fail(history["error"])
            time.sleep(0.025)
        self.fail(f"Assignment did not complete: {history}")

    def download(self, client, assignment, name):
        result = client.get(assignment + "/artifacts/" + name, params={"download": 1})
        self.assertEqual(result.status_code, 200, result.text)
        return result.text

    def test_verified_recovery_saves_method_and_fresh_run_retrieves_only_procedure(self):
        with TestClient(create_app(data_dir=self.data, model=RecoveryModel())) as client:
            assignment = self.submit(client, RECOVERY_REQUEST, self.inputs)
            recovered = self.wait_completed(client, assignment)
            receipts = tool_outputs(recovered["turns"][0]["steps"])
            original = quote_rows(receipts[f"read-{self.source}"])
            attempted = quote_rows(receipts[f"read-{LOSSY_UPLOAD_NAME}"])
            self.assertEqual([row.part_number for row in original], ["000731", "731"])
            self.assertEqual([row.part_number for row in attempted], ["731", "731"])
            self.assertEqual(receipts["total-NM-1"], {"result": "182.80"})
            self.assertEqual(receipts["total-NM-2"], {"result": "240.30"})
            self.assertIn("error", receipts["premature-method"])
            self.assertIn("error", receipts["unchecked-method"])
            self.assertEqual(receipts["save-method"], {
                "staged_method": METHOD_TITLE,
                "source_files": [self.source, LOSSY_UPLOAD_NAME],
                "checked_results": ["182.80", "240.30"],
                "corrected_artifact": CORRECTED_ARTIFACT,
            })
            corrected_csv = self.download(client, assignment, CORRECTED_ARTIFACT)
            self.assertEqual(
                [(row["part_number"], row["total_usd"], row["source"])
                 for row in csv.DictReader(io.StringIO(corrected_csv))],
                [("000731", "182.80", f"{self.source}:2"), ("731", "240.30", f"{self.source}:3")],
            )
            recommendation = self.download(client, assignment, "recommendation.md")
            self.assertIn("creating a collision", recommendation)
            self.assertIn("`000731`", recommendation)
            for name, content in self.inputs.items():
                self.assertEqual(client.get(f"{assignment}/uploads/{name}?download=1").content, content)

        fresh_model = FreshQuoteModel()
        with TestClient(create_app(data_dir=self.data, model=fresh_model)) as client:
            saved, = Store(self.data).list_methods()
            self.assertEqual(saved["source_task_id"], assignment.rsplit("/", 1)[-1])
            self.assertEqual(saved["title"], METHOD_TITLE)
            method_page = client.get(f"/methods/{saved['id']}")
            self.assertEqual(method_page.status_code, 200, method_page.text)
            self.assertIn("creating a collision", method_page.text)
            self.assertIn(assignment, method_page.text)

            fresh = self.submit(client, FRESH_REQUEST, FRESH_QUOTES)
            completed = self.wait_completed(client, fresh)
            self.assertNotEqual(fresh, assignment)
            self.assertEqual(len(completed["turns"]), 1)
            fresh_receipts = tool_outputs(completed["turns"][0]["steps"])
            self.assertEqual(fresh_receipts["method"], {
                "id": saved["id"], "title": METHOD_TITLE, "scope": METHOD_SCOPE, "procedure": METHOD_PROCEDURE,
            })
            self.assertIn(METHOD_PROCEDURE, "\n".join(fresh_model.requests))
            for old_context in (
                RECOVERY_REQUEST, recovered["response"], self.source, LOSSY_UPLOAD_NAME,
                "North Mill", "000731", "182.80", "240.30", "creating a collision", saved["source_task_id"],
            ):
                self.assertNotIn(old_context, "\n".join(fresh_model.requests))

            table = self.download(client, fresh, "comparison.csv")
            self.assertEqual(
                [(row["part_number"], row["total_usd"], row["source"])
                 for row in csv.DictReader(io.StringIO(table))],
                [
                    ("000582", "162.60", "alder-works.csv:2"), ("582", "133.40", "alder-works.csv:3"),
                    ("000582", "165.90", "beacon-metals.csv:2"), ("582", "116.50", "beacon-metals.csv:3"),
                ],
            )
            recommendation = self.download(client, fresh, "recommendation.md")
            self.assertIn("`000582`: choose Alder Works by $3.30", recommendation)
            self.assertIn("`582`: choose Beacon Metals by $16.90", recommendation)
            self.assertEqual(len(Store(self.data).list_methods()), 1)
            method_url = f"/methods/{saved['id']}"
            fields = {key: saved[key] for key in ("title", "scope", "procedure", "evidence")}
            fields["procedure"] += " Confirm the identifier column is text."
            edit = client.post(
                method_url, data={**fields, "expected_revision": str(saved["revision"])}, follow_redirects=False,
            )
            self.assertEqual(edit.status_code, 303, edit.text)
            self.assertIn(fields["procedure"], client.get(method_url).text)
            remove = client.post(
                method_url + "/remove", data={"expected_revision": str(saved["revision"] + 1)}, follow_redirects=False,
            )
            self.assertEqual(remove.status_code, 303, remove.text)
            self.assertEqual(client.get(method_url).status_code, 404)
            self.assertEqual(self.download(client, assignment, CORRECTED_ARTIFACT), corrected_csv)

    def test_stopped_and_superseded_recoveries_cannot_publish_staged_method(self):
        for action, status in (("stop", "stopped"), ("correct", "superseded")):
            with self.subTest(action=action):
                data = self.data / action
                model = RecoveryModel(hold_final=True)
                with TestClient(create_app(data_dir=data, model=model)) as client:
                    try:
                        assignment = self.submit(client, RECOVERY_REQUEST, self.inputs)
                        self.assertTrue(model.waiting.wait(5), self.history(client, assignment))
                        old, = self.history(client, assignment)["turns"]
                        staged = tool_outputs(old["steps"])["save-method"]
                        self.assertEqual(staged["staged_method"], METHOD_TITLE)
                        self.assertEqual(Store(data).list_methods(), [])
                        self.assertEqual(client.get(f"{assignment}/artifacts/{CORRECTED_ARTIFACT}").status_code, 404)

                        fields = {"request": CORRECTION_REQUEST} if action == "correct" else {}
                        result = client.post(
                            f"{assignment}/{action}", data={"turn_id": str(old["id"]), **fields},
                            follow_redirects=False,
                        )
                        self.assertEqual(result.status_code, 303, result.text)
                        self.assertEqual(self.history(client, assignment)["turns"][0]["status"], status)
                        model.release.set()
                        if action == "stop":
                            self.wait_completed(client, self.submit(client, MARKER_REQUEST))
                        else:
                            self.wait_completed(client, assignment)
                            self.assertEqual(model.correction_index, [])
                        self.assertTrue(model.final_returned.is_set())
                        end = self.history(client, assignment)["turns"][0]
                        self.assertEqual(end["status"], status)
                        self.assertEqual(end["steps"], old["steps"])
                        self.assertIsNone(end["response"])
                        self.assertEqual(Store(data).list_methods(), [])
                        self.assertNotIn(METHOD_TITLE, client.get("/methods").text)
                        self.assertEqual(client.get(f"{assignment}/artifacts/{CORRECTED_ARTIFACT}").status_code, 404)
                        self.assertEqual(client.get(f"{assignment}/artifacts/recommendation.md").status_code, 404)
                    finally:
                        model.release.set()
