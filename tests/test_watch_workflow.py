"""Two independent watches through the actual worker, SQLite, MCP transport and HTTP routes."""

import json
import os
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.quote_fixture import answer, call, tool_outputs
from tests.test_connected_workflow import Links, serve_source
from work_agent.storage import Store
from work_agent.web import create_app


DATA = Path(__file__).parent / "fixtures" / "watch-bulletin"
DECISION = "Watch Northstar's return authorization recommendation using the bulletin."
OPPORTUNITY = "Watch for Northstar funding opportunities in the bulletin and flag qualifying open calls."
SEED = "Remember the applicant eligibility rule in the Northstar project note."
ELIGIBILITY = "Northstar can apply only to opportunities expressly open to rural libraries."


class BulletinModel:
    """A deterministic provider reads only the current MCP snapshot and the worker-supplied memory."""

    def respond(self, history, tools, instructions, *, max_tool_calls):
        task = json.loads(next(item["content"] for item in reversed(history) if item.get("role") == "user"))
        outputs = tool_outputs(history)
        if task["request"] == SEED:
            if "source" not in outputs:
                items = [call("source", "read_file", file_id="charter.txt", start_line=1, line_count=1),
                         call("note", "read_project_note", scope="Northstar")]
            elif "save" not in outputs:
                items = [call("save", "stage_project_note", scope="Northstar", expected_revision=0,
                              reason="Applicant mandate", claims=[{"text": outputs["source"]["lines"][0]["text"],
                              "sources": ["../uploads/charter.txt#L1"], "supersedes": None}])]
            else:
                if "error" in outputs["save"]:
                    raise ValueError(outputs["save"]["error"])
                items = [answer("Recorded the mandate.")]
        elif "assessment" in outputs:
            if "error" in outputs["assessment"]:
                raise ValueError(outputs["assessment"]["error"])
            items = [answer("Check recorded.")]
        elif "bulletin" not in outputs:
            items = [call("bulletin", "mcp_bulletin__list_notices", cursor=0)]
            if task["watch"]["request"] != DECISION:
                claim = next((entry for entry in task.get("relevant_memory", {}).get("claims", [])
                              if entry["text"] == ELIGIBILITY), None)
                if not claim:
                    raise ValueError("Opportunity evaluator was not given its applicant mandate.")
                items.append(call("mandate", "read_cited_evidence", source_url=claim["sources"][0],
                                  start_line=0, line_count=1))
        else:
            watch = task["watch"]
            receipt = outputs["bulletin"]
            if "error" in receipt:
                assessment = {"outcome": "unavailable", "summary": "Bulletin could not be checked.",
                              "evidence": [], "findings": []}
            else:
                assessment = self.assess(watch, receipt, outputs.get("mandate"))
                if watch["request"] == DECISION and assessment["outcome"] == "material":
                    previous_url = watch["previous"]["evidence"][0]
                    if "prior_bulletin" not in outputs:
                        return {"status": "completed", "output": [call(
                            "prior_bulletin", "read_cited_evidence", source_url=previous_url,
                            start_line=0, line_count=1,
                        )]}
                    previous = outputs["prior_bulletin"]
                    if previous.get("source_url") != previous_url or "data" not in previous:
                        raise ValueError("The earlier bulletin snapshot could not be reopened.")
                    if any(tool.get("name") == "stage_evidence_change" for tool in tools):
                        raise ValueError("A snapshot-only watch was offered a numbered-line relationship.")
            items = [call("assessment", "stage_watch_assessment", **assessment)]
        return {"status": "completed", "output": items}

    @staticmethod
    def assess(watch, receipt, mandate):
        previous = watch["previous"]
        url = receipt["evidence_url"]
        evidence = [url]
        rows = receipt["data"]["records"]
        findings = {}
        if watch["request"] == DECISION:
            policy = [r for r in rows if r["kind"] == "policy" and r["status"] == "approved"][-1]
            summary = f"{policy['item_id']}: {policy['detail']}"
            if previous and summary != previous["summary"]:
                evidence += previous["evidence"][:1]
                key = "approval-" + policy["item_id"].lower()
                findings[key] = {"key": key, "title": "Return approval changed", "why": summary, "evidence": evidence}
        else:
            if mandate["lines"][0]["text"] != ELIGIBILITY:
                raise ValueError("The original mandate does not support this applicant.")
            evidence.append(mandate["source_url"] + "#L1")
            known = {event["key"] for event in watch["known_events"]}
            for row in rows:
                key = "opportunity-" + row["item_id"].lower()
                if (row["kind"] == "funding" and row["status"] == "open" and key not in known
                        and "rural libraries" in row["detail"].lower()):
                    findings.setdefault(key, {"key": key, "title": "Open opportunity " + row["item_id"],
                                              "why": row["detail"], "evidence": evidence})
            summary = f"Checked {len(rows)} bulletin records against the applicant mandate."
        outcome = "material" if findings else "unchanged" if previous else "baseline"
        return {"outcome": outcome, "summary": summary, "evidence": evidence, "findings": list(findings.values())}


class WatchWorkflowTests(unittest.TestCase):
    def app(self):
        return create_app(self.state, BulletinModel(), clock=lambda: datetime(2026, 11, 25, 18, tzinfo=UTC))

    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = folder / "state"
        self.bulletin = folder / "current.csv"
        self.bulletin.write_bytes((DATA / "initial.csv").read_bytes())
        source_url = self.enterContext(serve_source("bulletin", self.bulletin))
        config = folder / "sources.json"
        config.write_text(json.dumps({"sources": [{"name": "bulletin", "url": source_url, "tools": ["list_notices"]}]}))
        self.enterContext(patch.dict(os.environ, {"WORK_AGENT_MCP_CONFIG": str(config)}))

    def append(self, filename):
        with self.bulletin.open("ab") as out:
            out.write((DATA / filename).read_bytes())

    def completed(self, client, response):
        self.assertEqual(response.status_code, 303, response.text)
        path = response.headers["location"].split("/tasks/")[-1]
        path = "/tasks/" + path
        deadline = time.monotonic() + 10
        while True:
            history = client.get(path + "/history").json()
            if history["status"] == "completed":
                return path, tool_outputs(history["steps"])
            self.assertIn(history["status"], ("running", "queued"), history.get("error") or history)
            self.assertLess(time.monotonic(), deadline, history)
            time.sleep(0.02)

    def create(self, client, request):
        response = client.post("/watches", data={"request": request, "frequency": "daily", "local_time": "07:30"},
                               follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        return "/watches/" + response.headers["location"].rsplit("/", 1)[1]

    def check(self, client, watch):
        return self.completed(client, client.post(watch + "/check-now", follow_redirects=False))

    def outcomes(self, watch):
        checks = Store(self.state).get_watch(int(watch.rsplit("/", 1)[1]))["checks"]
        return [check["outcome"] for check in reversed(checks)]

    def seed(self, client):
        path, _ = self.completed(client, client.post("/tasks", data={"request": SEED},
                                files={"files": ("charter.txt", ELIGIBILITY, "text/plain")}, follow_redirects=False))
        return path

    def test_watch_decision_and_discover_new_opportunity_across_restart(self):
        with TestClient(self.app()) as client:
            seed = self.seed(client)
            decision = self.create(client, DECISION)
            grants = self.create(client, OPPORTUNITY)
            baseline, out = self.check(client, decision)
            old_url = baseline + out["bulletin"]["evidence_url"].removeprefix("..")
            self.check(client, grants)
            self.append("drafts.csv")
            for watch in (decision, grants):
                self.check(client, watch)
            self.assertNotIn("Needs your attention", client.get("/").text)
            self.append("published.csv")
            changed, out = self.check(client, decision)
            new_url = changed + out["bulletin"]["evidence_url"].removeprefix("..")
            self.assertNotIn("P-21", {row["item_id"] for row in out["prior_bulletin"]["data"]["records"]})
            self.assertIn("P-21", {row["item_id"] for row in out["bulletin"]["data"]["records"]})
            self.check(client, grants)
            homepage = client.get("/").text
            for watch in (decision, grants):
                self.assertEqual(self.outcomes(watch), ["baseline", "unchanged", "material"])
            self.assertIn("Return approval changed", homepage)
            self.assertIn("Open opportunity G-203", homepage)
            self.assertNotIn("Open opportunity G-101", homepage)
            for href in (old_url, new_url):
                self.assertIn(href, Links(client.get(decision).text).hrefs)
            before = [client.get(u).content for u in (old_url, new_url)]
            self.assertNotIn(b"Amends P-19", before[0])
            self.assertIn(b"Amends P-19", before[1])
            self.assertIn(ELIGIBILITY, client.get(seed + "/uploads/charter.txt").text)
        with TestClient(self.app()) as client:
            self.assertEqual([client.get(u).content for u in (old_url, new_url)], before)
            self.append("mirror.csv")
            self.check(client, grants)
            self.check(client, decision)
            home = client.get("/").text
            self.assertEqual(home.count("Open opportunity G-203"), 1)
            self.assertEqual(home.count("Return approval changed"), 1)
            working = self.bulletin.read_bytes()
            self.bulletin.unlink()
            _, outage = self.check(client, grants)
            self.assertEqual(outage["bulletin"]["error_kind"], "source_tool_error")
            self.assertNotIn("arguments", outage["bulletin"]["error"].lower())
            self.assertNotIn("evidence_url", outage["bulletin"])
            self.assertNotIn("data", outage["bulletin"])
            self.assertIn("Bulletin could not be checked.", client.get(grants).text)
            self.bulletin.write_bytes(working)
            self.check(client, grants)
            self.assertEqual(self.outcomes(grants)[-3:], ["unchanged", "unavailable", "unchanged"])
            self.assertEqual(client.get("/").text.count("Open opportunity G-203"), 1)
            # Another watch created after publication must surface a currently qualifying first result.
            fresh = self.create(client, OPPORTUNITY + " Include qualifying calls already open today.")
            self.check(client, fresh)
            self.assertEqual(self.outcomes(fresh), ["material"])
            self.assertIn("Open opportunity G-203", client.get(fresh).text)
