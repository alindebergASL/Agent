import json
import unittest

import httpx

from work_agent.connected_sources import MAX_RESULT_BYTES
from work_agent.github_source import create_server


REPOSITORY = "example/project"
BASE = "/repos/" + REPOSITORY
GITHUB = "https://github.com/" + REPOSITORY
MAY_1 = "2026-05-01T12:00:00Z"
MAY_2 = "2026-05-02T12:00:00Z"
MAY_3 = "2026-05-03T12:00:00Z"


def pull(number, updated_at=MAY_3, *, state="open", merged_at=None):
    return {
        "number": number, "title": f"Change {number}", "state": state, "draft": False,
        "body": "Implementation complete. Live verification is pending.",
        "user": {"login": "builder"}, "created_at": MAY_1, "updated_at": updated_at,
        "closed_at": updated_at if state == "closed" else None, "merged_at": merged_at,
        "merge_commit_sha": "b" * 40, "head": {"sha": "a" * 40, "ref": "feature"},
        "base": {"ref": "main"}, "html_url": f"{GITHUB}/pull/{number}",
        "requested_reviewers": [{"login": "reviewer"}], "requested_teams": [],
    }


class GitHubSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = []
        self.serve = None

        def route(request):
            self.requests.append(request)
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.scheme, "https")
            self.assertEqual(request.url.host, "api.github.com")
            self.assertTrue(request.url.path.startswith(BASE + "/"))
            self.assertNotIn("Authorization", request.headers)
            return self.serve(request)

        self.server = create_server(REPOSITORY, transport=httpx.MockTransport(route))

    async def read(self, tool, **arguments):
        result = await self.server.call_tool(tool, arguments)
        self.assertFalse(result.is_error)
        return result.structured_content

    async def test_repository_scoped_read_tools(self):
        tools = {tool.name: tool for tool in await self.server.list_tools()}
        self.assertEqual(set(tools), {"list_pull_requests", "get_pull_request", "list_pull_request_notes"})
        for tool in tools.values():
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            self.assertTrue(set(tool.input_schema["properties"]).isdisjoint({"url", "repository", "owner", "token"}))
        for repo in ("example/project/extra", "../project", "https://example.com/project", "example/%2e%2e"):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                create_server(repo)
        self.assertEqual(self.requests, [])

    async def test_page_boundary_reuses_fixed_endpoint_and_reports_real_merges(self):
        malicious_next = '<https://unrelated.invalid/other-repo?offset=20>; rel="next"'

        def serve(request):
            self.assertEqual(request.url.path, BASE + "/pulls")
            self.assertEqual(request.url.params["sort"], "updated")
            self.assertEqual(request.url.params["direction"], "desc")
            if request.url.params["page"] == "1":
                return httpx.Response(200, json=[pull(2), pull(1, MAY_2, state="closed", merged_at=MAY_2)],
                                      headers={"Link": malicious_next})
            return httpx.Response(200, json=[pull(3, MAY_1, state="closed")])

        self.serve = serve
        first = await self.read("list_pull_requests")
        self.assertEqual(first["repository"], REPOSITORY)
        self.assertTrue(first["observed_at"])
        self.assertEqual(first["next_page"], 2)
        opened, merged = first["records"]
        self.assertEqual((opened["state"], opened["merge_commit_sha"]), ("open", None))
        self.assertEqual((merged["state"], merged["merge_commit_sha"]), ("merged", "b" * 40))
        self.assertEqual(merged["url"], f"{GITHUB}/pull/1")
        last = await self.read("list_pull_requests", page=first["next_page"])
        self.assertIsNone(last["next_page"])
        closed, = last["records"]
        self.assertEqual((closed["state"], closed["merge_commit_sha"]), ("closed", None))
        self.assertEqual(len(self.requests), 2)

    async def test_since_filters_older_prs_even_when_github_has_another_page(self):
        def serve(request):
            return httpx.Response(200, json=[pull(3), pull(2, MAY_2), pull(1, MAY_1)],
                                  headers={"Link": '<https://api.github.com/unused>; rel="next"'})

        self.serve = serve
        page = await self.read("list_pull_requests", updated_since=MAY_2)
        self.assertEqual([r["number"] for r in page["records"]], [3, 2])
        self.assertIsNone(page["next_page"])

    async def test_merge_commit_found_on_later_timeline_page_when_absent_from_pr(self):
        pr = pull(11, state="closed", merged_at=MAY_3)
        del pr["merge_commit_sha"]
        merged_sha = "c" * 40

        def serve(request):
            if request.url.path == BASE + "/pulls/11":
                return httpx.Response(200, json=pr)
            self.assertEqual(request.url.path, BASE + "/issues/11/timeline")
            if request.url.params["page"] == "1":
                return httpx.Response(200, json=[{"event": "committed", "sha": pr["head"]["sha"]}],
                                      headers={"Link": '<https://api.github.com/unused>; rel="next"'})
            return httpx.Response(200, json=[{
                "id": 761, "event": "merged", "actor": {"login": "maintainer"},
                "created_at": MAY_3, "commit_id": merged_sha,
            }])

        self.serve = serve
        detail = (await self.read("get_pull_request", number=11))["record"]
        self.assertEqual((detail["state"], detail["merge_commit_sha"]), ("merged", None))
        args = {"number": 11, "kind": "merge_events"}
        first = await self.read("list_pull_request_notes", **args)
        self.assertEqual((first["records"], first["next_page"]), ([], 2))
        last = await self.read("list_pull_request_notes", **args, page=first["next_page"])
        merge, = last["records"]
        self.assertIsNone(last["next_page"])
        self.assertEqual(merge["event"], "merged")
        self.assertEqual(merge["commit_id"], merged_sha)
        self.assertNotEqual(merge["commit_id"], detail["head_sha"])
        self.assertEqual((merge["author"], merge["url"]), ("maintainer", f"{GITHUB}/commit/{merged_sha}"))

    async def test_separate_authored_review_and_verification_evidence(self):
        detail = pull(11, state="closed", merged_at=MAY_3)
        comment = {"id": 141, "user": {"login": "operator"}, "body": "Live EC2 test passed on " + "a" * 40,
                   "created_at": MAY_2, "updated_at": MAY_3, "html_url": f"{GITHUB}/pull/11#issuecomment-141"}
        review = {"id": 88, "state": "APPROVED", "commit_id": "a" * 40, "user": {"login": "reviewer"},
                  "body": "Reviewed the code.", "submitted_at": MAY_2, "html_url": f"{GITHUB}/pull/11#pullrequestreview-88"}
        payloads = {
            BASE + "/pulls/11": detail,
            BASE + "/issues/11/comments": [comment],
            BASE + "/pulls/11/reviews": [review],
        }
        self.serve = lambda request: httpx.Response(200, json=payloads[request.url.path])
        pr = (await self.read("get_pull_request", number=11))["record"]
        self.assertEqual(pr["body"], detail["body"])
        self.assertEqual(pr["head_sha"], review["commit_id"])
        self.assertIn("reviewer", pr["requested_reviewers"])
        comments = await self.read("list_pull_request_notes", number=11, kind="comments")
        c, = comments["records"]
        self.assertEqual((c["author"], c["body"], c["url"], c["updated_at"]),
                         ("operator", comment["body"], comment["html_url"], MAY_3))
        reviews = await self.read("list_pull_request_notes", number=11, kind="reviews")
        r, = reviews["records"]
        self.assertEqual((r["author"], r["state"], r["commit_id"], r["body"], r["url"]),
                         ("reviewer", "APPROVED", review["commit_id"], review["body"], review["html_url"]))
        self.assertEqual(len(self.requests), 3)

    async def test_long_unicode_bodies_can_be_read_in_full(self):
        detail = {**pull(12), "body": "完" * 4_000 + "Verified the current head on EC2."}
        self.serve = lambda request: httpx.Response(200, json=detail)
        pr = (await self.read("get_pull_request", number=12))["record"]
        self.assertTrue(pr["body_truncated"])
        self.assertLess(len(pr["body"]), len(detail["body"]))
        tail = (await self.read("get_pull_request", number=12, body_offset=pr["next_body_offset"]))["record"]
        self.assertFalse(tail["body_truncated"])
        self.assertEqual(pr["body"] + tail["body"], detail["body"])

    async def test_full_page_of_escaped_review_text_fits_the_connected_tool_budget(self):
        body = ('\t"\\\n' * 1_000) + "結果"
        rows = [{"id": i, "user": {"login": "reviewer"}, "body": body, "state": "COMMENTED",
                 "commit_id": "a" * 40, "submitted_at": MAY_2} for i in range(10)]
        self.serve = lambda request: httpx.Response(200, json=rows)
        parts = []
        offset = 0
        while offset is not None:
            page = await self.read("list_pull_request_notes", number=11, kind="reviews", body_offset=offset)
            size = len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode())
            self.assertLessEqual(size, MAX_RESULT_BYTES)
            self.assertEqual(len(page["records"]), 10)
            review = page["records"][0]
            parts.append(review["body"])
            next_offset = review["next_body_offset"]
            self.assertTrue(next_offset is None or next_offset > offset)
            offset = next_offset
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), body)

    async def test_limit_denial_and_redirect_are_not_empty_successes(self):
        for status, headers in [(429, {"Retry-After": "60"}), (403, {"X-RateLimit-Remaining": "0"}),
                                (404, {}), (307, {"Location": "https://unrelated.invalid/path"})]:
            with self.subTest(status=status):
                self.serve = lambda request: httpx.Response(status, headers=headers, json={"message": "raw upstream failure"})
                result = await self.read("list_pull_requests")
                self.assertTrue(result["error"])
                if status in (403, 429):
                    self.assertIn("rate limit", result["error"].lower())
                self.assertNotIn("records", result)
                self.assertNotIn("raw upstream failure", result["error"])
        self.assertEqual(len(self.requests), 4)
