"""Paired purchasing and release decisions through the assignment UI and normal worker."""

import argparse
import csv
import io
import json
import os
import sys
import tempfile
import time
from collections import Counter
from contextlib import nullcontext
from decimal import Decimal, InvalidOperation
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import OpenAIModel
from work_agent.storage import Store
from work_agent.web import create_app


MODEL = "gpt-6.2-sol"
FREIGHT_METHOD = "Apply supplier freight terms"
RELEASE_METHOD = "Check migration release gates"
CORPUS = [
    (FREIGHT_METHOD, "Compare delivered supplier quotes for a purchase order with shipment-based charges.",
     "Read the current quote rows. Use unit price times quantity. Multiply freight per shipment by shipment "
     "count and freight per order by order count. Compare the resulting delivered costs and cite the terms."),
    (RELEASE_METHOD, "Production release approval for schema migrations.",
     "Check the latest applicable release policy. Verify the release type and restore drill outcome against the "
     "migration exception before approving. Cite the controlling policy and the current release evidence."),
    *[(f"Review {topic}", f"Review {topic} documentation.", f"Verify the primary {topic} document.")
      for topic in ("translations", "orchards", "algebra", "archaeology", "satellites", "photo archives",
                    "patent renewals", "lecture transcripts")],
    ("Classify ocean freight surcharges", "Container shipping surcharge audits on ocean routes.",
     "Separate the documented container surcharge from the monthly carrier adjustment."),
]
# Independently specified expected decisions; include a quote revision that flips the winner.
FREIGHT_CASES = {
    "split": (2, 30, (1060, 1050)),
    "single": (1, 30, (1030, 1050)),
    "revised": (2, 12, (1024, 1050)),
}
RELEASE_CASES = {"drill_failed": ("failed", "hold", "repeat_restore_drill"),
                 "drill_passed": ("passed", "ship", "deploy")}
CASES = (*FREIGHT_CASES, *RELEASE_CASES)
SUPPLIERS = ("North", "South")
FIELDS = ("supplier", "total_usd", "winner", "source")
SOURCE = "offers.csv"
ARTIFACT = "decision.csv"


def inputs(case):
    if case in RELEASE_CASES:
        drill, _, _ = RELEASE_CASES[case]
        request = (
            "Should we release Osprey today? Apply the current release policy. Save decision.csv with one row "
            "and columns decision,next_action,rule_source,evidence_source. Write ship or hold for decision. "
            "Use deploy, repeat_restore_drill or request_approval for next_action. Cite filename:line in each "
            "source field."
        )
        evidence = (
            "# Release control record\nStandard releases may ship after two approvals.\n"
            "For schema migrations, shipping requires two approvals and a passing restore drill. If the drill "
            "fails, hold and repeat the drill.\n"
            f"Osprey changes the database schema. Two approvals are recorded. The latest restore drill {drill}.\n"
        )
        return request, "release.md", evidence.encode()
    shipments, freight, _ = FREIGHT_CASES[case]
    request = (
        f"Which supplier should we choose for 10 units on one purchase order delivered in {shipments} "
        "shipment(s)? Compare the attached quotes. Save decision.csv with one row per supplier and exactly "
        "these columns: supplier,total_usd,winner,source. Use yes or no for winner and filename:line for source."
    )
    source = (f"supplier,unit_price,freight,freight_basis\nNorth,100,{freight},per shipment\n"
              "South,103,20,per order\n").encode()
    return request, SOURCE, source


def seed(store):
    store.initialize()
    task = store.submit("Curate reusable procedures", {})
    if store.claim() != task:
        raise RuntimeError("Unable to claim the evaluation seed assignment.")
    store.complete(task, "Verified the procedures.")
    for title, scope, procedure in CORPUS:
        store.save_method(task, title, scope, procedure, "Verified during curation.")


class FixtureModel:
    """Exercise tools and scoring deterministically; offers no evidence of model quality."""

    def __init__(self, case):
        self.case = case

    def respond(self, history, tools, instructions, *, max_tool_calls):
        request = json.loads(history[0]["content"])
        results = tool_outputs(history)
        title = FREIGHT_METHOD if self.case in FREIGHT_CASES else RELEASE_METHOD
        target = next((m for m in request.get("saved_methods", []) if m["title"] == title), None)
        if target and "procedure" not in results:
            output = [call("procedure", "read_method", method_id=target["id"])]
        elif "source" not in results:
            output = [call("source", "read_file", file_id=request["uploaded_files"][0], start_line=1, line_count=10)]
        elif "file" in results:
            output = [answer("Saved decision.csv.")]
        elif self.case in RELEASE_CASES:
            lines = results["source"]["lines"]
            passed = "passed" in lines[3]["text"]
            decision = "ship,deploy" if passed else "hold,repeat_restore_drill"
            content = f"decision,next_action,rule_source,evidence_source\n{decision},release.md:3,release.md:4\n"
            output = [call("file", "write_artifact", name=ARTIFACT, content=content)]
        elif "sum0" not in results:
            rows = csv.DictReader(row["text"] for row in results["source"]["lines"])
            output = []
            for i, row in enumerate(rows):
                factor = FREIGHT_CASES[self.case][0] if row["freight_basis"] == "per shipment" else 1
                output.append(call(f"sum{i}", "calculate",
                                   expression=f"10*{row['unit_price']}+{factor}*{row['freight']}"))
        else:
            totals = [Decimal(results[f"sum{i}"]["result"]) for i in range(len(SUPPLIERS))]
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(FIELDS)
            for i, supplier in enumerate(SUPPLIERS):
                writer.writerow([supplier, totals[i], "yes" if totals[i] == min(totals) else "no", f"{SOURCE}:{i+2}"])
            output = [call("file", "write_artifact", name=ARTIFACT, content=stream.getvalue())]
        return {"status": "completed", "output": output}


class ObservedModel:
    def __init__(self, model):
        self.model = model
        self.offered = []
        self.retrieved = set()
        self.source_rows = set()
        self.provider_models = set()
        self.usage = Counter()

    def respond(self, history, tools, instructions, *, max_tool_calls):
        self.offered = json.loads(history[0]["content"]).get("saved_methods", [])
        for item in history:
            if item["type"] == "function_call_output":
                data = json.loads(item["output"])
                if isinstance(data, dict) and {"id", "procedure", "scope"} <= data.keys():
                    self.retrieved.add(data["id"])
                if isinstance(data, dict) and "file_id" in data and "lines" in data:
                    self.source_rows.update(f'{data["file_id"]}:{row["number"]}' for row in data["lines"])
        response = self.model.respond(history, tools, instructions, max_tool_calls=max_tool_calls)
        if response.get("model"):
            self.provider_models.add(response["model"])
        usage = response.get("usage") or {}
        for name in ("input_tokens", "output_tokens"):
            self.usage[name] += usage.get(name) or 0
        return response


def score(case, csv_text):
    if case in RELEASE_CASES:
        _, expected_decision, expected_action = RELEASE_CASES[case]
        try:
            row, = list(csv.DictReader(io.StringIO(csv_text)))
            return {"decision": row["decision"].strip().casefold() == expected_decision,
                    "action": row["next_action"].strip().casefold() == expected_action,
                    "citations": (row["rule_source"].strip(), row["evidence_source"].strip()) ==
                                 ("release.md:3", "release.md:4")}
        except (csv.Error, ValueError, KeyError, AttributeError):
            return dict.fromkeys(("decision", "action", "citations"), False)
    expected = FREIGHT_CASES[case][2]
    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        if set(reader.fieldnames or ()) != set(FIELDS) or len(rows) != len(SUPPLIERS):
            raise ValueError("Missing or unexpected rows or columns")
        by_supplier = {row["supplier"].strip().casefold(): row for row in rows}
        results = [by_supplier[supplier.casefold()] for supplier in SUPPLIERS]
        totals = [Decimal(row["total_usd"].strip().replace("$", "").replace(",", "")) for row in results]
        return {
            "totals": totals == list(expected),
            "winner": all(row["winner"].strip().casefold() == ("yes" if total == min(expected) else "no")
                          for row, total in zip(results, expected)),
            "citations": all(row["source"].strip() == f"{SOURCE}:{i+2}" for i, row in enumerate(results)),
        }
    except (csv.Error, InvalidOperation, ValueError, TypeError, AttributeError, KeyError):
        return dict.fromkeys(("totals", "winner", "citations"), False)


def recent_eight(store, request, context):
    return store.list_methods(limit=8)


def evaluate(case, arm, mode):
    request, filename, source = inputs(case)
    target_id = 1 if case in FREIGHT_CASES else 2
    model = (OpenAIModel(os.environ["OPENAI_API_KEY"], MODEL, base_url="https://api.openai.com/v1")
             if mode == "live" else FixtureModel(case))
    observed = ObservedModel(model)
    policy = patch.object(Store, "relevant_methods", recent_eight) if arm == "recent-eight" else nullcontext()
    with tempfile.TemporaryDirectory() as root, policy, patch.dict(os.environ, {"WORK_AGENT_MCP_CONFIG": ""}):
        store = Store(Path(root))
        seed(store)
        with TestClient(create_app(Path(root), observed)) as client:
            submitted = client.post("/tasks", data={"request": request},
                                    files=[("files", (filename, source, "text/plain"))], follow_redirects=False)
            if submitted.status_code != 303:
                raise RuntimeError(f"Assignment form returned {submitted.status_code}.")
            task = submitted.headers["location"].split("/")[-1]
            deadline = time.monotonic() + 335
            while time.monotonic() < deadline:
                state = store.get_task(task)
                if state["status"] not in ("queued", "running"):
                    break
                time.sleep(.05 if mode == "fixture" else .5)
            saved = store.read_artifact(task, ARTIFACT)
            csv_text = saved.decode("utf-8-sig") if saved else ""
            checks = score(case, csv_text)
            cited_lines = (2, 3) if case in FREIGHT_CASES else (3, 4)
            checks["sources_read"] = all(f"{filename}:{n}" in observed.source_rows for n in cited_lines)
            return {
                "case": case, "policy": arm, "status": state["status"], "error": state.get("error"),
                "offered_ids": [row["id"] for row in observed.offered], "read_ids": sorted(observed.retrieved),
                "target_method_read": target_id in observed.retrieved, "checks": checks,
                "decision_pass": state["status"] == "completed" and all(checks.values()),
                "observed_source_rows": sorted(observed.source_rows), "output_csv": csv_text, "provider_models": sorted(observed.provider_models),
                "usage": dict(observed.usage),
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.live and not os.environ.get("OPENAI_API_KEY"):
        parser.error("Live comparison requires the existing OPENAI_API_KEY in this container.")
    runs = []
    mode_name = "live" if args.live else "fixture"
    report = {"mode": mode_name, "requested_model": MODEL if args.live else None, "runs": runs}
    for repeat in range(args.repeats):
        for n, case in enumerate(CASES):
            arms = ["recent-eight", "relevance"]
            if (repeat + n) % 2:
                arms.reverse()
            for arm in arms:
                print(f"{mode_name}: pair {repeat + 1} / {case} / {arm}", file=sys.stderr, flush=True)
                result = evaluate(case, arm, mode_name)
                runs.append({"repeat": repeat + 1, **result})
    summary = {}
    for policy in ("recent-eight", "relevance"):
        group = [row for row in runs if row["policy"] == policy]
        summary[policy] = {"runs": len(group), "decisions_passed": sum(row["decision_pass"] for row in group),
                           "target_reads": sum(row["target_method_read"] for row in group)}
    report["summary"] = summary
    print(json.dumps(report, indent=2))
    return int(any(row["status"] != "completed" for row in runs))


if __name__ == "__main__":
    raise SystemExit(main())
