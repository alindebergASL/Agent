"""Paired unprompted routing decisions using evidence-backed project memory."""

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from fastapi.testclient import TestClient

from tests.method_policy_eval import MODEL, ObservedModel
from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import OpenAIModel
from work_agent.storage import Store
from work_agent.web import create_app


# The first two cases have the exact same request and different evidence.
# The other cases check that applying one exception never changes all routing.
CASES = {
    "original": ("Larkspur Archive", "cobalt", "V14", "base"),
    "exception": ("Larkspur Archive", "cobalt", "J23", "cobalt"),
    "outside-exception": ("Larkspur Archive", "plain", "V14", "plain"),
    "other-facility": ("Cedar Archive", "cobalt", "K81", "cedar"),
}


def publish(store, scope, filename, rules, revision=0):
    source = ("Approved routing record: " + scope + "\n" + "\n".join(rules) + "\n").encode()
    task = store.submit(f"Record the approved {scope} intake procedure.", {filename: source})
    if store.claim() != task:
        raise RuntimeError("Could not start the seed assignment.")
    claims = [
        {"text": text, "sources": [f"../uploads/{filename}#L{i}"]}
        for i, text in enumerate(rules, start=2)
    ]
    reason = "Approved intake process." if not revision else "Signed amendment changes intake of cobalt-sealed crates."
    store.stage_project_note(task, store.get_run(task)["turn_id"], scope, revision, claims, reason)
    store.complete(task, reason)
    return [claim["sources"][0] for claim in store.read_project_note(scope)["claims"]]


def seed(path, case):
    store = Store(path)
    store.initialize()
    base, = publish(store, "Larkspur Archive", "receiving.txt", ["Route all incoming crates to V14."])
    sources = {"base": base}
    if case != "original":
        sources["cobalt"], sources["plain"] = publish(store, "Larkspur Archive", "amendment.txt", [
            "Route cobalt-sealed crates to J23.", "Route every other crate to V14.",
        ], revision=1)
    sources["cedar"], = publish(store, "Cedar Archive", "receiving.txt", ["Route every incoming crate to K81."])
    return sources[CASES[case][3]]


def request_for(scope, seal):
    return (f"Prepare tomorrow's receiving slip for a {seal}-sealed crate arriving at {scope}. "
            "Save slip.csv with one row and columns desk_code,source. Use the desk code and the exact "
            "workspace source-line URL that supports the routing.")


class Fixture:
    """Exercise lookup, revision selection and the scorer; it is not a reasoning benchmark."""

    def respond(self, history, tools, instructions, *, max_tool_calls):
        user = json.loads(history[0]["content"])
        results = tool_outputs(history)
        name = re.search(r"arriving at (.+?)\.", user["request"])[1]
        if "write" in results:
            output = answer("Saved the receiving slip.")
        elif "source" in results:
            source = results["source"]
            line = source["lines"][0]
            desk = re.search(r"\b[A-Z][0-9]{2}\b", line["text"])[0]
            url = source["revision_url"] + "#L" + str(line["number"])
            output = call("write", "write_artifact", name="slip.csv", content=f"desk_code,source\n{desk},{url}\n")
        else:
            available = [claim for claim in user.get("relevant_memory", {}).get("claims", [])
                         if claim["scope"] == name]
            if "note" in results:
                available = results["note"]["claims"]
            if available:
                cobalt = "cobalt-sealed crate" in user["request"]
                claim = next((c for c in available if cobalt == ("cobalt-sealed" in c["text"])), available[0])
                output = call("source", "read_cited_evidence", source_url=claim["sources"][0],
                              start_line=0, line_count=1)
            elif "discover" not in results:
                output = call("discover", "search_project_notes", query=name)
            else:
                target = next(n for n in results["discover"]["notes"] if n["scope"] == name)
                output = call("note", "read_project_note", scope=target["scope"])
        return {"status": "completed", "output": [output]}


def measure(store, task, scope, expected_source, desk):
    history = store.run_history(task)
    saved = store.read_artifact(task, "slip.csv")
    text = saved.decode("utf-8-sig") if saved else ""
    action_ok = citation_ok = False
    try:
        row, = list(csv.DictReader(io.StringIO(text)))
        action_ok = row["desk_code"].strip().casefold() == desk.casefold()
        citation_ok = store.resolve_note_source(task, row["source"].strip()) == expected_source
    except (csv.Error, ValueError, TypeError, AttributeError, KeyError):
        pass

    read_urls = set()
    for data in tool_outputs(history["steps"]).values():
        if not isinstance(data, dict) or not data.get("lines"):
            continue
        base = data.get("source_url")
        if not base and data.get("kind") == "upload" and data.get("source_task_id"):
            base = f"/tasks/{data['source_task_id']}/uploads/{quote(data['file_id'], safe='')}"
        if base:
            read_urls.update(f"{base}#L{line['number']}" for line in data["lines"])
    return {"case_scope": scope, "status": history["status"], "error": history.get("error"),
            "action_correct": action_ok, "exact_source_cited": citation_ok,
            "controlling_line_read": expected_source in read_urls, "read_urls": sorted(read_urls), "slip": text}


def evaluate(path, case, arm, mode, expected_source):
    scope, seal, desk, _ = CASES[case]
    model = (OpenAIModel(os.environ["OPENAI_API_KEY"], MODEL, base_url="https://api.openai.com/v1")
             if mode == "live" else Fixture())
    observed = ObservedModel(model)
    # Keep the memory-use instructions and prompt structure identical, including for an empty preview.
    empty_preview = {"claims": [], "candidates": []}
    prior = (patch.object(Store, "relevant_project_memory", return_value=empty_preview)
             if arm == "on-demand" else nullcontext())
    with prior, patch.dict(os.environ, {"WORK_AGENT_MCP_CONFIG": ""}), TestClient(create_app(path, observed)) as client:
        submitted = client.post("/tasks", data={"request": request_for(scope, seal)}, follow_redirects=False)
        if submitted.status_code != 303:
            raise RuntimeError(f"Assignment form returned {submitted.status_code}.")
        task = submitted.headers["location"].split("/")[-1]
        store = Store(path)
        deadline = time.monotonic() + 335
        while store.get_task(task)["status"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(.05 if mode == "fixture" else .5)
        result = measure(store, task, scope, expected_source, desk)
        disclosures = [event for event in store.run_history(task)["steps"] if event.get("type") == "memory_context"]
        shown = [claim for event in disclosures for claim in event.get("claims", [])]
        result.update(controlling_claim_shown=any(expected_source in claim.get("sources", []) for claim in shown),
                      shown_scopes=sorted({c["scope"] for c in shown}), provider_models=sorted(observed.provider_models),
                      usage=dict(observed.usage))
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1 or (args.live and not os.environ.get("OPENAI_API_KEY")):
        parser.error("Use positive repeats and configure the existing development credential for a live run.")
    mode_name = "live" if args.live else "fixture"
    rows = []
    for repeat in range(args.repeats):
        for index, case in enumerate(CASES):
            with tempfile.TemporaryDirectory() as tmp:
                baseline = Path(tmp) / "seed"
                expected = seed(baseline, case)
                arms = ["on-demand", "proactive"]
                if (repeat + index) % 2:
                    arms.reverse()
                for arm in arms:
                    path = Path(tmp) / arm
                    shutil.copytree(baseline, path)
                    print(f"{mode_name}: {repeat + 1}/{case}/{arm}", file=sys.stderr, flush=True)
                    rows.append({"repeat": repeat + 1, "case": case, "arm": arm,
                                 **evaluate(path, case, arm, mode_name, expected)})
    summary = {}
    for arm in ("on-demand", "proactive"):
        group = [r for r in rows if r["arm"] == arm]
        summary[arm] = {"trials": len(group), **{key: sum(r[key] for r in group) for key in (
            "controlling_claim_shown", "controlling_line_read", "action_correct", "exact_source_cited")}}
    print(json.dumps({"mode": mode_name, "requested_model": MODEL if args.live else None,
                      "summary": summary, "runs": rows}, indent=2))
    return int(any(row["status"] != "completed" for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
