import json
import unittest
from contextlib import contextmanager
from copy import deepcopy
from threading import Lock
from unittest.mock import Mock

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import MAX_TOOL_CALLS, AssignmentError, run_assignment
from work_agent.storage import InactiveTurn


ORDERS = "mcp_orders__list"
HOSTED = {"type": "program", "caller_id": "p1"}


def response(*items):
    return {"status": "completed", "output": list(items)}


def catalog():
    return Mock(tools=[{
        "type": "function", "name": ORDERS, "parameters": {"type": "object", "properties": {}},
        "allowed_callers": ["direct", "programmatic"],
    }], warnings=[])


class ConnectedRunnerTests(unittest.TestCase):
    def test_direct_and_hosted_calls_keep_individual_evidence_and_native_replay(self):
        source = catalog()
        source.call.side_effect = [
            {"source": "orders", "tool": "list", "data": {"ids": ["001"], "next": "next-page"}},
            {"source": "orders", "tool": "list", "data": {"ids": ["1"], "next": None}},
        ]
        source.warnings = ["Inventory source unavailable."]
        script = iter((
            response(call("first", ORDERS)),
            response({**call("second", ORDERS, cursor="next-page"), "caller": HOSTED}),
            response(answer("Read both pages.")),
        ))
        frames = []

        def respond(history, tools, instructions, *, max_tool_calls):
            self.assertIn(source.tools[0], tools)
            frames.append(deepcopy(history))
            return next(script)

        events = []
        model = Mock()
        model.respond.side_effect = respond
        final = run_assignment(model, "Count orders.", {}, Mock(), Mock(), events.append, connected_catalog=source)
        self.assertEqual(final, "Read both pages.")
        self.assertEqual([c.args for c in source.call.call_args_list], [(ORDERS, {}), (ORDERS, {"cursor": "next-page"})])
        self.assertEqual(json.loads(frames[0][0]["content"])["connected_source_warnings"], source.warnings)
        native = [item for item in frames[-1] if item["type"] == "function_call_output"]
        durable = [item for item in events if item["type"] == "function_call_output"]
        self.assertEqual(len(durable), 2)
        self.assertNotEqual(durable[0]["source_ref"], durable[1]["source_ref"])
        for frame, step, ids in zip(native, durable, (["001"], ["1"]), strict=True):
            ref = step["source_ref"]
            self.assertRegex(ref, r"^[a-f0-9]{32}$")
            self.assertEqual(frame, {k: v for k, v in step.items() if k != "source_ref"})
            payload = json.loads(frame["output"])
            self.assertEqual(payload["evidence_url"], f"../evidence/{ref}")
            self.assertEqual(payload["data"]["ids"], ids)
            self.assertEqual((payload["source"], payload["tool"]), ("orders", "list"))
        self.assertEqual(native[1]["caller"], HOSTED)

    def test_only_advertised_reads_dispatch_and_failures_do_not_become_evidence(self):
        source = catalog()
        source.call.return_value = {"error": "The source could not complete the read."}
        model = Mock()
        model.respond.side_effect = [
            response(
                {**call("unknown", "mcp_orders__delete"), "caller": HOSTED},
                {**call("arguments", ORDERS), "arguments": "[]"},
                call("failed", ORDERS),
            ),
            response(answer("The source is unavailable.")),
        ]
        events = []
        run_assignment(model, "Read orders.", {}, Mock(), Mock(), events.append, connected_catalog=source)
        source.call.assert_called_once_with(ORDERS, {})
        self.assertEqual(tool_outputs(events)["arguments"]["error_kind"], "invalid_arguments")
        for step in (item for item in events if item["type"] == "function_call_output"):
            self.assertNotIn("source_ref", step)
            self.assertIn("error", json.loads(step["output"]))
        self.assertEqual(model.respond.call_args.kwargs["max_tool_calls"], MAX_TOOL_CALLS - 3)

        model.respond.side_effect = [response(*(call(str(n), ORDERS) for n in range(MAX_TOOL_CALLS + 1)))]
        with self.assertRaisesRegex(AssignmentError, "tool call limit"):
            run_assignment(model, "Read orders.", {}, Mock(), Mock(), Mock(), connected_catalog=source)
        source.call.assert_called_once()

    def test_stop_during_slow_read_discards_receipt_and_remaining_calls(self):
        source = catalog()
        lock = Lock()
        active = True

        @contextmanager
        def guard():
            with lock:
                if not active:
                    raise InactiveTurn("Stopped.")
                yield

        def read(name, arguments):
            nonlocal active
            self.assertTrue(lock.acquire(blocking=False), "Stop must be able to acquire the turn lock during a read.")
            try:
                active = False
            finally:
                lock.release()
            return {"source": "orders", "tool": "list", "data": ["late record"]}

        source.call.side_effect = read
        model = Mock()
        model.respond.return_value = response(
            {**call("started", ORDERS), "caller": HOSTED}, call("late-read", ORDERS),
            call("late-write", "write_artifact", name="order.md", content="late"),
        )
        stage = Mock()
        events = []
        with self.assertRaises(InactiveTurn):
            run_assignment(model, "Prepare a report.", {}, stage, Mock(), events.append,
                           connected_catalog=source, tool_scope=guard)
        stage.assert_not_called()
        source.call.assert_called_once()
        model.respond.assert_called_once()
        self.assertEqual(tool_outputs(events), {})

    def test_program_reads_saved_revision_without_authorizing_replacement(self):
        name = "history.csv"
        original = b"item,qty\n0031,5\n"
        snapshots = {name: {"revision": 3, "author": "user"}}
        def read(call_id):
            return call(call_id, "read_saved_artifact", name=name, start_line=1, line_count=10)

        model = Mock()
        model.respond.side_effect = [
            response({**read("hosted"), "caller": HOSTED}),
            response(call("early", "write_artifact", name=name, content="truncated")),
            response(read("direct")),
            response(call("after", "write_artifact", name=name, content=original.decode() + "0001,8\n")),
            response(answer("Saved.")),
        ]
        events = []
        stage = Mock()
        run_assignment(model, "Add one row.", {}, stage, Mock(), events.append,
                       saved_artifacts=snapshots, read_saved_artifact=Mock(return_value=original))
        advertised = [t for t in model.respond.call_args.args[1] if t.get("name") == "read_saved_artifact"]
        self.assertEqual(advertised[0]["allowed_callers"], ["direct", "programmatic"])
        outputs = tool_outputs(events)
        self.assertEqual(outputs["hosted"], outputs["direct"])
        self.assertEqual(outputs["hosted"]["revision"], 3)
        self.assertIn("directly", outputs["early"]["error"])
        self.assertEqual(outputs["after"]["staged"], name)
        stage.assert_called_once_with(name, original.decode() + "0001,8\n")
