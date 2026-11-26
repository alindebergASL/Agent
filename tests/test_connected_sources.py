import json
from contextlib import asynccontextmanager
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio
import httpx2

from work_agent.connected_sources import ConnectedSources, MAX_RESULT_BYTES


def tool(name, **hints):
    return {"name": name, "inputSchema": {"type": "object", "properties": {"page": {"type": "integer"}}},
            "annotations": hints}


class ConnectedSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"TEST_SOURCE_BEARER": "only-the-source-should-see-this"}))
        self.requests = []
        self.result = {"content": [], "structuredContent": {"records": [{"po": "000731"}]}}
        self.rpc_error = None
        self.pages = [
            {"tools": [tool("other", readOnlyHint=True), tool("edit", readOnlyHint=False)], "nextCursor": "more"},
            {"tools": [tool("lookup", readOnlyHint=True), tool("destructive", readOnlyHint=True, destructiveHint=True)]},
        ]

        def route(request):
            self.requests.append(request)
            rpc = json.loads(request.content)
            method = rpc["method"]
            if request.url.host == "redirect.example":
                return httpx2.Response(307, headers={"location": "https://untrusted.example/mcp"})
            if method == "server/discover":
                response = {"error": {"code": -32601, "message": "Unknown method"}}
            elif method == "initialize":
                response = {"result": {"capabilities": {"tools": {}}, "protocolVersion": "2025-11-25",
                                       "serverInfo": {"name": "demo", "version": "1"}}}
            elif method == "notifications/initialized":
                return httpx2.Response(202)
            elif method == "tools/list":
                response = {"result": self.pages[bool(rpc.get("params", {}).get("cursor"))]}
            elif method == "tools/call":
                self.assertEqual(rpc["params"]["name"], "lookup")
                response = {"error": self.rpc_error} if self.rpc_error else {"result": self.result}
            else:
                raise AssertionError(method)
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": rpc["id"], **response})

        real_client = httpx2.AsyncClient
        self.enterContext(patch("work_agent.connected_sources.httpx2.AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=httpx2.MockTransport(route), **kwargs)))

    def config(self, *sources):
        path = Path(self.temp) / "sources.json"
        path.write_text(json.dumps({"sources": list(sources)}))
        return ConnectedSources.from_file(path)

    def test_pages_permissions_namespaces_and_results(self):
        left = {"name": "left", "url": "https://left.example/mcp", "tools": ["lookup", "edit", "destructive"],
                "token_env": "TEST_SOURCE_BEARER"}
        right = {"name": "right", "url": "https://right.example/mcp", "tools": ["lookup"]}
        self.pages[1]["tools"][0]["outputSchema"] = {"type": "object", "properties": {"records": {"type": "array"}}}
        with self.config(left, right).open() as catalog:
            self.assertEqual([t["name"] for t in catalog.tools], ["mcp_left__lookup", "mcp_right__lookup"])
            self.assertEqual(catalog.tools[0]["allowed_callers"], ["direct", "programmatic"])
            schema = catalog.tools[0]["output_schema"]
            self.assertEqual(schema["properties"]["data"], self.pages[1]["tools"][0]["outputSchema"])
            self.assertEqual(schema["properties"]["evidence_url"], {"type": "string"})
            self.assertIn({"required": ["error"]}, schema["anyOf"])
            self.assertEqual(catalog.tools[0]["parameters"]["properties"]["page"], {"type": "integer"})
            args = {"page": 1}
            expected_data = {"records": [{"po": "000731"}]}
            self.assertEqual(catalog.call("mcp_left__lookup", args), {
                "source": "left", "tool": "lookup", "data": expected_data,
            })
            self.assertEqual(catalog.call("mcp_right__lookup", args)["data"], expected_data)
            self.assertIn("error", catalog.call("mcp_left__edit", {}))
            self.assertIn("error", catalog.call("mcp_left__other", {}))
            self.assertIn("error", catalog.call("mcp_left__destructive", {}))
            self.assertTrue(all("only-the-source" not in str(x) for x in [catalog.tools, catalog.warnings]))
        for r in self.requests:
            if r.url.host == "left.example":
                self.assertEqual(r.headers["Authorization"], "Bearer only-the-source-should-see-this")
            else:
                self.assertNotIn("Authorization", r.headers)

    def test_bad_sources_isolated_and_no_credentials_follow_redirect(self):
        with self.config(
            {"name": "redirect", "url": "https://redirect.example/mcp", "tools": ["lookup"],
             "token_env": "TEST_SOURCE_BEARER"},
            {"name": "missing", "url": "https://missing.example/mcp", "tools": ["lookup"],
             "token_env": "NONEXISTENT_TEST_ENV"},
            {"name": "ok", "url": "http://ok.example/mcp", "tools": ["lookup"]},
        ).open() as catalog:
            self.assertEqual([t["name"] for t in catalog.tools], ["mcp_ok__lookup"])
            self.assertEqual(len(catalog.warnings), 2)
            self.assertIn("Source missing is unavailable because its configured credential is missing.", catalog.warnings)
        self.assertEqual({r.url.host for r in self.requests}, {"redirect.example", "ok.example"})

    def test_limits_remote_errors_and_text(self):
        with self.config({"name": "demo", "url": "https://left.example/mcp", "tools": ["lookup"],
                          "token_env": "TEST_SOURCE_BEARER"}).open() as catalog:
            def read():
                return catalog.call("mcp_demo__lookup", {})
            self.result = {"content": [{"type": "text", "text": "Book 0731"}]}
            self.assertEqual(read()["data"], "Book 0731")
            self.result = {"content": [{"type": "text", "text": "x" * (MAX_RESULT_BYTES + 1)}]}
            self.assertIn("smaller page", read()["error"])
            self.result = {"content": [{"type": "text", "text": os.environ["TEST_SOURCE_BEARER"]}]}
            self.assertNotIn(os.environ["TEST_SOURCE_BEARER"], read()["error"])
            self.result["isError"] = True
            failed = read()
            self.assertEqual(failed["error_kind"], "source_tool_error")
            self.assertNotIn("arguments", failed["error"])
            self.assertNotIn(os.environ["TEST_SOURCE_BEARER"], json.dumps(failed))
            self.assertNotIn("data", failed)

    def test_protocol_errors_use_only_reported_codes(self):
        secret = os.environ["TEST_SOURCE_BEARER"]
        with self.config({"name": "demo", "url": "https://left.example/mcp", "tools": ["lookup"]}).open() as catalog:
            for code, expected in [(-32602, "invalid_arguments"), (-32001, "timeout"),
                                   (-32000, "transport_error"), (-32603, "protocol_error")]:
                self.rpc_error = {"code": code, "message": secret, "data": {"private": secret}}
                failed = catalog.call("mcp_demo__lookup", {})
                with self.subTest(code=code):
                    self.assertEqual(failed["error_kind"], expected)
                    self.assertNotIn(secret, json.dumps(failed))
                    self.assertNotIn("data", failed)

    def test_slow_discovery_is_bounded_and_releases_the_worker(self):
        async def stall(*_, **__):
            await anyio.sleep_forever()

        sources = self.config({"name": "slow", "url": "http://source.example/mcp", "tools": ["lookup"]})
        with patch("work_agent.connected_sources.READ_TIMEOUT", 0.1), patch("mcp.Client.list_tools", new=stall):
            with sources.open() as catalog:
                self.assertEqual(catalog.warnings, ["Source slow is unavailable."])
                self.assertEqual(catalog.tools, [])

    def test_timed_out_tool_read_can_recover(self):
        async def stall(*_, **__):
            await anyio.sleep_forever()

        sources = self.config({"name": "demo", "url": "https://left.example/mcp", "tools": ["lookup"]})
        with sources.open() as catalog:
            with patch("work_agent.connected_sources.READ_TIMEOUT", 0.1), patch("mcp.ClientSession.call_tool", new=stall):
                failed = catalog.call("mcp_demo__lookup", {})
            self.assertEqual(failed["error_kind"], "timeout")
            self.assertNotIn("data", failed)
            self.assertEqual(catalog.call("mcp_demo__lookup", {})["data"], self.result["structuredContent"])

    def test_close_failure_does_not_overwrite_assignment_outcome(self):
        @asynccontextmanager
        async def failing_close(source):
            yield None, {}, None
            raise RuntimeError("peer disconnected")

        sources = self.config({"name": "a", "url": "http://source.example/mcp", "tools": ["lookup"]})
        with patch("work_agent.connected_sources._open_source", failing_close):
            with sources.open() as catalog:
                self.assertEqual(catalog.tools, [])
            with self.assertRaisesRegex(ValueError, "original task error"), sources.open():
                raise ValueError("original task error")

    def test_rejects_ambiguous_names_and_insecure_credentials(self):
        base = {"name": "a", "url": "http://source.example/mcp", "tools": ["lookup"]}
        for update in ({"name": "a__other"}, {"url": "file:///source"}, {"url": "https://name:password@source/"},
                       {"token_env": "TEST_SOURCE_BEARER"}, {"tools": ["lookup", "lookup"]}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.config({**base, **update})
        with self.assertRaises(ValueError):
            self.config(base, base)
