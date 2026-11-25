"""Exercise recurring runs with a model stand-in, real MCP HTTP and a mutable GitHub fixture."""

import json
import os
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urljoin

import httpx
from fastapi.testclient import TestClient

from tests.quote_fixture import answer, call, tool_outputs
from tests.test_connected_workflow import Links, serve_mcp
from tests.test_github_status_workflow import GitHubFixture, HEAD, PULL, REPO, REPORT
from work_agent.github_source import create_server
from work_agent.storage import Store
from work_agent.web import create_app


class RecurringBriefModel:
    def respond(self, history, tools, instructions, *, max_tool_calls):
        outputs = tool_outputs(history)
        if "save" in outputs:
            items = [answer("Saved the project brief.")]
        elif "notes" in outputs:
            notes = outputs["notes"]
            records = notes["data"]["records"]
            pull, = outputs["index"]["data"]["records"]
            report = [f"[PR #{pull['number']}]({pull['url']})"]
            for note in records:
                report.append(f"{note['author']} reported {note['body']} [Comment]({note['url']})")
            report.append(f"[Current source]({notes['evidence_url']})")
            if "prior" in outputs:
                prior = outputs["prior"]
                previous_text = "\n".join(line["text"] for line in prior["lines"])
                new_notes = [note for note in records if f"]({note['url']})" not in previous_text]
                report.append("New verification comment." if new_notes else "No new verification comments.")
                report.append(f"[Prior brief]({prior['revision_url']})")
            else:
                report.append("First brief.")
            items = [call("save", "write_artifact", name=REPORT, content="\n\n".join(report))]
        elif "index" in outputs:
            pull, = outputs["index"]["data"]["records"]
            items = [call("notes", "mcp_github__list_pull_request_notes", kind="comments", number=pull["number"])]
        else:
            request = json.loads(next(item["content"] for item in reversed(history) if item.get("role") == "user"))
            items = [call("index", "mcp_github__list_pull_requests", state="open")]
            if request.get("prior_artifacts"):
                ref, = request["prior_artifacts"]
                items.append(call("prior", "read_prior_artifact", reference_id=ref["reference_id"],
                                  start_line=1, line_count=100))
        return {"status": "completed", "output": items}


class RecurringContinuityTests(unittest.TestCase):
    def wait_for_run(self, client, store, previous_task=None):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run = store.get_schedule()["latest_run"]
            if run and run["task_id"] != previous_task:
                task = f"/tasks/{run['task_id']}"
                history = client.get(task + "/history").json()
                if history["status"] == "failed":
                    self.fail(history["error"])
                if history["status"] == "completed":
                    self.assertEqual(history["uploads"], [])
                    self.assertEqual(len(history["turns"]), 1)
                    return task, tool_outputs(history["steps"])
            time.sleep(0.025)
        self.fail("A fresh occurrence did not finish.")

    def test_manual_and_scheduled_runs_compare_fresh_evidence_with_the_previous_revision(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state = folder / "state"
        store = Store(state)
        upstream = GitHubFixture()
        mcp = create_server(REPO, transport=httpx.MockTransport(upstream.read))
        before_due = datetime(2026, 11, 24, 16, tzinfo=UTC)
        due = datetime(2026, 11, 24, 17, tzinfo=UTC)
        with serve_mcp(mcp) as source:
            config = folder / "sources.json"
            config.write_text(json.dumps({"sources": [{"name": "github", "url": source,
                "tools": ["list_pull_requests", "list_pull_request_notes"]}]}))
            with patch.dict(os.environ, {"WORK_AGENT_MCP_CONFIG": str(config)}):
                with TestClient(create_app(state, RecurringBriefModel(), clock=lambda: before_due)) as client:
                    creation = client.post("/responsibility", data={
                        "request": "Save a project status brief. Tell me what changed.",
                        "weekday": "1", "local_time": "09:00",
                    }, follow_redirects=False)
                    self.assertEqual(creation.status_code, 303, creation.text)
                    schedule = store.get_schedule()
                    run_now = f"/responsibility/{schedule['id']}/run-now"
                    self.assertEqual(client.post(run_now, follow_redirects=False).status_code, 303)
                    first, initial = self.wait_for_run(client, store)
                    self.assertNotIn("prior", initial)
                    first_artifact = f"{first}/artifacts/{REPORT}"
                    first_bytes = client.get(first_artifact, params={"download": 1}).content
                    self.assertIn(b"First brief.", first_bytes)

                    upstream.comments.append({
                        "id": 9001, "user": {"login": "tester"}, "html_url": PULL + "#issuecomment-9001",
                        "body": f"live verification at {HEAD}.",
                        "created_at": "2026-11-24T16:00:00Z", "updated_at": "2026-11-24T16:00:00Z",
                    })
                    self.assertEqual(client.post(run_now, follow_redirects=False).status_code, 303)
                    second, changed = self.wait_for_run(client, store, first.rsplit("/", 1)[1])
                    second_artifact = f"{second}/artifacts/{REPORT}"
                    changed_page = client.get(second_artifact)
                    self.assertIn("New verification comment.", changed_page.text)
                    self.assertIn(PULL + "#issuecomment-9001", Links(changed_page.text).hrefs)
                    prior = changed["prior"]
                    self.assertEqual((prior["source_task_id"], prior["revision"]), (first.rsplit("/", 1)[1], 1))
                    self.assertEqual("\n".join(line["text"] for line in prior["lines"]).encode(), first_bytes)
                    self.assertIn(prior["revision_url"], Links(changed_page.text).hrefs)
                    first_link = urljoin(second_artifact, prior["revision_url"])
                    first_evidence = urljoin(first_artifact, initial["notes"]["evidence_url"])
                    self.assertNotIn(b"issuecomment-9001", client.get(first_evidence).content)
                    self.assertEqual(client.post(first_artifact, data={"expected_revision": "1", "content":
                        "Edited after the next occurrence had already finished."}, follow_redirects=False).status_code, 303)
                    self.assertEqual(store.get_schedule()["next_run_at"], schedule["next_run_at"])

                # The worker starts the quiet run automatically when the saved weekly time arrives.
                with TestClient(create_app(state, RecurringBriefModel(), clock=lambda: due)) as client:
                    third, quiet = self.wait_for_run(client, store, second.rsplit("/", 1)[1])
                    third_artifact = f"{third}/artifacts/{REPORT}"
                    self.assertEqual(store.get_schedule()["latest_run"]["manual"], 0)
                    self.assertEqual(len({first, second, third}), 3)
                    self.assertEqual(quiet["prior"]["source_task_id"], second.rsplit("/", 1)[1])
                    second_bytes = client.get(second_artifact, params={"download": 1}).content
                    self.assertEqual("\n".join(line["text"] for line in quiet["prior"]["lines"]).encode(), second_bytes)
                    self.assertIn("No new verification comments.", client.get(third_artifact).text)
                    second_evidence = urljoin(second_artifact, changed["notes"]["evidence_url"])

        with TestClient(create_app(state, clock=lambda: due)) as client:
            baseline_page = client.get(first_link)
            self.assertEqual(baseline_page.url.path, first_artifact)
            self.assertEqual(baseline_page.url.params["revision"], "1")
            self.assertIn("First brief.", baseline_page.text)
            self.assertNotIn("Edited after", baseline_page.text)
            self.assertIn("Edited after", client.get(first_artifact).text)
            self.assertIn(initial["notes"]["evidence_url"], Links(baseline_page.text).hrefs)
            original_receipt = client.get(urljoin(str(baseline_page.url), initial["notes"]["evidence_url"]))
            self.assertEqual(original_receipt.url.path, first_evidence)
            self.assertEqual(original_receipt.status_code, 200)
            self.assertNotIn(b"issuecomment-9001", original_receipt.content)
            self.assertIn(b"issuecomment-9001", client.get(second_evidence).content)
            assignment_page = client.get(second)
            self.assertIn(urljoin(str(assignment_page.url), first_link), Links(assignment_page.text).hrefs)
            self.assertEqual(client.get(f"{first}/prior-artifacts/{prior['reference_id']}").status_code, 404)
