"""Run two assignment turns with real MCP transport, a GitHub REST fixture and a model stand-in."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urljoin

import httpx
from fastapi.testclient import TestClient

from tests.quote_fixture import answer, call, tool_outputs
from tests.test_connected_workflow import Links, serve_mcp
from work_agent.github_source import create_server
from work_agent.web import create_app


REPO = "example/project"
PULL = "https://github.com/example/project/pull/42"
HEAD = "c" * 40
REPORT = "project-brief.md"


class GitHubFixture:
    def __init__(self):
        self.comments = []

    def read(self, request):
        if request.method != "GET":
            raise AssertionError(request.method)
        if request.url.path in (f"/repos/{REPO}/pulls", f"/repos/{REPO}/pulls/42"):
            pull = {
                "number": 42, "title": "Connected research", "html_url": PULL,
                "draft": False, "state": "open", "merged": False, "merged_at": None,
                "merge_commit_sha": None, "head": {"sha": HEAD}, "base": {"ref": "main"},
                "user": {"login": "builder"}, "body": "A live check is required for this pull request.",
                "requested_reviewers": [], "created_at": "2026-11-23T10:00:00Z",
                "updated_at": "2026-11-24T10:00:00Z" if self.comments else "2026-11-23T10:00:00Z",
            }
            return httpx.Response(200, json=[pull] if request.url.path.endswith("/pulls") else pull)
        if request.url.path == f"/repos/{REPO}/issues/42/comments":
            return httpx.Response(200, json=self.comments)
        raise AssertionError(request.url.path)


class StatusModel:
    """Build a predictable Markdown artifact from the current turn's real tool outputs."""

    def respond(self, history, tools, instructions, *, max_tool_calls):
        outputs = tool_outputs(history)
        task = json.loads(next(item["content"] for item in reversed(history) if item.get("role") == "user"))
        if "save" in outputs:
            items = [answer("Saved the project brief.")]
        elif "pull" in outputs:
            pull = outputs["pull"]["data"]["record"]
            notes = outputs["notes"]["data"]["records"]
            report = [
                f"PR #{pull['number']} is {pull['state']}.", f"[Pull request]({pull['url']})",
                f"[Recorded state]({outputs['pull']['evidence_url']})",
            ]
            if notes:
                note, = notes
                report.extend([f"{note['author']} reported: {note['body']}", f"[Comment]({note['url']})"])
            else:
                report.append("No verification comment was found.")
            report.append(f"[Recorded comments]({outputs['notes']['evidence_url']})")
            if "previous" in outputs:
                prior = [line["text"] for line in outputs["previous"]["lines"]]
                if "No verification comment was found." in prior and notes:
                    report.append("New since the previous brief: the live check was reported.")
            items = [call("save", "write_artifact", name=REPORT, content="\n\n".join(report))]
        elif "index" in outputs:
            pull, = outputs["index"]["data"]["records"]
            items = [call("pull", "mcp_github__get_pull_request", number=pull["number"]),
                     call("notes", "mcp_github__list_pull_request_notes", number=pull["number"], kind="comments")]
        else:
            items = [call("index", "mcp_github__list_pull_requests", state="all")]
            if task.get("saved_artifacts"):
                items.append(call("previous", "read_saved_artifact", name=REPORT, start_line=1, line_count=60))
        return {"status": "completed", "output": items}


class GitHubStatusWorkflowTests(unittest.TestCase):
    def wait_for_turn(self, client, url, count):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            history = client.get(url + "/history").json()
            if history["status"] == "failed":
                self.fail(history["error"])
            if len(history["turns"]) == count and history["status"] == "completed":
                self.assertEqual(history["uploads"], [])
                return tool_outputs(history["turns"][-1]["steps"])
            time.sleep(0.025)
        self.fail(f"Turn {count} did not finish.")

    def test_follow_up_compares_saved_brief_and_cites_new_comment(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state = folder / "state"
        upstream = GitHubFixture()
        server = create_server(REPO, transport=httpx.MockTransport(upstream.read))
        with serve_mcp(server) as url:
            config = folder / "sources.json"
            config.write_text(json.dumps({"sources": [{
                "name": "github", "url": url,
                "tools": ["list_pull_requests", "get_pull_request", "list_pull_request_notes"],
            }]}))
            with (
                patch.dict(os.environ, {"WORK_AGENT_MCP_CONFIG": str(config)}),
                TestClient(create_app(state, StatusModel())) as client,
            ):
                first = client.post("/tasks", data={"request": "How are we doing? Save a project status brief."},
                                    follow_redirects=False)
                self.assertEqual(first.status_code, 303, first.text)
                task = first.headers["location"]
                initial = self.wait_for_turn(client, task, 1)
                artifact = task + "/artifacts/" + REPORT
                baseline = client.get(artifact, params={"download": 1}).content
                self.assertIn(b"No verification comment was found.", baseline)
                self.assertEqual(initial["notes"]["data"]["records"], [])
                comment_url = PULL + "#issuecomment-9001"
                upstream.comments.append({
                    "id": 9001, "html_url": comment_url, "user": {"login": "tester"},
                    "body": f"ARM64 live check passed at {HEAD}.",
                    "created_at": "2026-11-24T10:00:00Z", "updated_at": "2026-11-24T10:00:00Z",
                })
                follow = client.post(task + "/follow-up", data={"request": "Update the brief. What changed?"},
                                     follow_redirects=False)
                self.assertEqual(follow.status_code, 303, follow.text)
                self.assertEqual(follow.headers["location"], task)
                current = self.wait_for_turn(client, task, 2)
                self.assertEqual(current["previous"]["revision"], 1)
                page = client.get(artifact)
                self.assertIn("New since the previous brief: the live check was reported.", page.text)
                self.assertIn("PR #42 is open.", page.text)
                self.assertIn(f"tester reported: ARM64 live check passed at {HEAD}.", page.text)
                self.assertIn(comment_url, Links(page.text).hrefs)
                snapshots = []
                for revision, outputs in enumerate((initial, current), start=1):
                    href = outputs["notes"]["evidence_url"]
                    self.assertIn(href, Links(client.get(artifact, params={"revision": revision}).text).hrefs)
                    evidence_url = urljoin(artifact, href)
                    snapshot = client.get(evidence_url)
                    self.assertEqual(snapshot.status_code, 200)
                    self.assertEqual(b"ARM64 live check passed" in snapshot.content, revision == 2)
                    snapshots.append((evidence_url, snapshot.content))
                self.assertNotEqual(snapshots[0][0], snapshots[1][0])
                self.assertEqual(client.get("/tasks/other/" + href.removeprefix("../")).status_code, 404)

        with TestClient(create_app(state, StatusModel())) as client:
            self.assertEqual(client.get(artifact, params={"revision": 1, "download": 1}).content, baseline)
            for evidence_url, content in snapshots:
                self.assertEqual(client.get(evidence_url).content, content)
