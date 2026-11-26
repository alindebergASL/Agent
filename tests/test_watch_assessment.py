import unittest
from unittest.mock import Mock

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import AssignmentError, run_assignment
from work_agent.watch_assessment import WatchAssessment


POLICY = "https://example.edu/policy"
DRAFT = "https://example.edu/draft"
NOTE = "/notes/4?revision=3"
SNAPSHOT = "../evidence/" + "a" * 32
TASK = "b" * 32


def receipt(outcome, *urls, findings=None):
    return {"outcome": outcome, "summary": "Visitor policy check.", "findings": findings or [], "evidence": list(urls)}


def web(url, status="completed"):
    return {"type": "web_search_call", "id": "ws1", "status": status, "action": {"type": "open_page", "url": url}}


def completed(*items):
    return {"status": "completed", "output": list(items)}


class WatchAssessmentTests(unittest.TestCase):
    def test_explicit_sources_need_actual_visits_and_failed_visit_can_be_retried(self):
        w = WatchAssessment({"request": "Watch the policy and draft. Skip https://example.edu/obsolete.",
                             "required_urls": [POLICY, DRAFT]}, TASK)
        w.observe_web(web(POLICY))
        w.observe_web({"type": "web_search_call", "status": "completed",
                       "action": {"type": "search", "sources": [{"url": DRAFT}]}})
        with self.assertRaisesRegex(ValueError, "Visit every"):
            w.validate(receipt("baseline", POLICY))
        w.observe_web(web(DRAFT, "failed"))
        self.assertEqual(w.validate(receipt("unavailable", POLICY))["evidence"], [POLICY])
        w.observe_web(web(DRAFT))
        self.assertEqual(w.validate(receipt("baseline", POLICY, DRAFT))["evidence"], [POLICY, DRAFT])
        with self.assertRaisesRegex(ValueError, "observed"):
            w.validate(receipt("baseline", POLICY + "/invented"))

    def test_material_finding_cannot_reuse_only_past_or_archived_evidence(self):
        previous = {"outcome": "baseline", "evidence": [POLICY]}
        w = WatchAssessment({"request": "Monitor policy", "previous": previous}, TASK)
        w.observe("search_watch_findings", {"findings": [{"evidence": [NOTE]}]}, {})
        w.observe("read_cited_evidence", {"source_url": POLICY, "data": {}}, {})
        with self.assertRaisesRegex(ValueError, "fresh"):
            w.validate(receipt("unchanged", POLICY, NOTE))
        w.observe_web(web(DRAFT))
        finding = {"title": "Rule", "key": "adopted-rule", "why": "Visitor instructions change.", "evidence": [POLICY]}
        with self.assertRaisesRegex(ValueError, "Each finding needs fresh"):
            w.validate(receipt("material", POLICY, DRAFT, findings=[finding]))
        finding["evidence"].append(DRAFT)
        self.assertEqual(w.validate(receipt("material", POLICY, DRAFT, findings=[finding]))["findings"], [finding])

    def test_mcp_application_error_and_unread_pages_prevent_clean_assessment(self):
        w = WatchAssessment({"request": "Watch the connected catalog"}, TASK)
        tool = "mcp_catalog__read"
        p1, p2 = {"page": 1, "scope": "arts"}, {"page": 2, "scope": "arts"}
        def read(args, data, url=SNAPSHOT):
            w.observe(tool, {"evidence_url": url, "data": data}, args, connected=True)
        read(p1, {"records": [], "next_page": 2})
        with self.assertRaisesRegex(ValueError, "unread pages"):
            w.validate(receipt("baseline", SNAPSHOT))
        read(p2, {"error": "Source quota exhausted."})
        with self.assertRaisesRegex(ValueError, "failed"):
            w.validate(receipt("baseline", SNAPSHOT))
        read(p2, {"records": [], "next_page": None}, "../evidence/" + "c"*32)
        self.assertEqual(w.validate(receipt("baseline", SNAPSHOT))["evidence"],
                         [f"/tasks/{TASK}/evidence/" + "a" * 32])

    def test_order_independent_pagination(self):
        w = WatchAssessment({"request": "Watch my records"}, TASK)
        for page, nxt in ((2, None), (1, 2)):
            w.observe("mcp_catalog__list", {"data": {"next_page": nxt}, "evidence_url": SNAPSHOT},
                      {"page": page}, True)
        self.assertEqual(w.validate(receipt("baseline", SNAPSHOT))["outcome"], "baseline")

    def test_workspace_current_claims_and_page_coverage(self):
        w = WatchAssessment({"request": "Watch workspace note"}, TASK)
        def observe(n):
            w.observe("observe_watch_subject", {"observed_workspace_subject": True, "source_url": NOTE,
                      "total_lines": 2, "lines": [{"number": n, "text": "Claim", "claim_url": NOTE + f"#claim-{n}"}]}, {})
        observe(1)
        with self.assertRaisesRegex(ValueError, "Finish reading"):
            w.validate(receipt("baseline", NOTE))
        observe(2)
        self.assertEqual(w.validate(receipt("baseline", NOTE + "#claim-1"))["evidence"], [NOTE + "#claim-1"])
        with self.assertRaisesRegex(ValueError, "observed"):
            w.validate(receipt("baseline", NOTE + "#L1"))

    def test_attached_subject_cannot_be_replaced_with_a_fresh_unrelated_page(self):
        w = WatchAssessment({"request": "Watch the project decision.", "subject": {"note_id": 4}}, TASK)
        w.observe_web(web(POLICY))
        for outcome in ("baseline", "material", "unchanged"):
            findings = [{"key": "change", "title": "Change", "why": "Relevant", "evidence": [POLICY]}] if outcome == "material" else []
            with self.subTest(outcome=outcome), self.assertRaisesRegex(ValueError, "attached current"):
                w.validate(receipt(outcome, POLICY, findings=findings))
        self.assertEqual(w.validate(receipt("unavailable", POLICY))["outcome"], "unavailable")

    def test_partial_subject_page_cannot_be_hidden_by_citing_only_the_public_source(self):
        w = WatchAssessment({"request": "Monitor the decision.", "subject": {"note_id": 4}}, TASK)
        w.observe_web(web(POLICY))
        w.observe("observe_watch_subject", {"observed_workspace_subject": True, "source_url": NOTE,
                  "total_lines": 2, "lines": [{"number": 1, "claim_url": NOTE + "#claim-1"}]}, {})
        with self.assertRaisesRegex(ValueError, "Finish reading"):
            w.validate(receipt("baseline", POLICY))
        w.context["previous"] = {"outcome": "baseline", "evidence": [POLICY, "/notes/4?revision=2"]}
        with self.assertRaisesRegex(ValueError, "Finish reading"):
            w.validate(receipt("unchanged", POLICY))
        w.context["previous"]["evidence"] = [NOTE + "#claim-2"]
        self.assertEqual(w.validate(receipt("unchanged", POLICY))["outcome"], "unchanged")

    def test_premature_final_is_repaired_before_publishing(self):
        model, stage = Mock(), Mock()
        model.respond.side_effect = [
            completed(answer("Nothing changed.")),
            completed(web(POLICY), call("finish", "stage_watch_assessment", **receipt("baseline", POLICY))),
        ]
        result = run_assignment(model, "Monitor the policy", {}, Mock(), Mock(), Mock(),
                                watch_context={"request": "Monitor the policy"}, task_id=TASK, stage_watch_assessment=stage)
        self.assertEqual(result, "Visitor policy check.")
        stage.assert_called_once_with(**receipt("baseline", POLICY))
        model.respond.side_effect = [completed(answer("Done.")), completed(answer("Done."))]
        with self.assertRaisesRegex(AssignmentError, "without a verified"):
            run_assignment(model, "Monitor", {}, Mock(), Mock(), Mock(), watch_context={"request": "Monitor"},
                           task_id=TASK, stage_watch_assessment=stage)
        self.assertEqual(stage.call_count, 1)

    def test_disabled_on_ordinary_assignments_and_no_argument_read_without_subject(self):
        model = Mock()
        model.respond.return_value = completed(answer("Done."))
        run_assignment(model, "Ordinary work", {}, Mock(), Mock(), Mock())
        tools = {t.get("name") for t in model.respond.call_args.args[1]}
        self.assertNotIn("stage_watch_assessment", tools)
        self.assertNotIn("observe_watch_subject", tools)
        records = []
        model.respond.side_effect = [
            completed(call("guessed", "observe_watch_subject", start_line=1, line_count=1)),
            completed(call("finish", "stage_watch_assessment", **receipt("unavailable"))),
        ]
        run_assignment(model, "Watch", {}, Mock(), Mock(), records.append, watch_context={"request": "Watch"},
                       task_id=TASK, stage_watch_assessment=Mock())
        self.assertIn("unavailable", tool_outputs(records)["guessed"]["error"])
