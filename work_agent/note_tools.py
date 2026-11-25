"""Evidence-bearing tools for reading and revising shared project notes."""

import json

from .cited_evidence import line_source_urls
from .project_notes import clean_scope


def schemas(tool):
    claim_fields = {
        "text": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
        "supersedes": {
            "type": ["integer", "null"],
            "description": "1-based number of the claim this replaces in the preceding revision. Otherwise null.",
        },
    }
    return [
        tool(
            "search_project_notes",
            "Find account or project notes by scope. Read a matching scope to see its claims.",
            {"query": {"type": "string"}},
        ),
        tool(
            "read_project_note",
            "Read the current claims and evidence in one scope. A new scope returns revision zero.",
            {"scope": {"type": "string"}},
        ),
        tool(
            "read_project_note_revision",
            "Read the claims, evidence and rationale in an earlier note revision.",
            {"scope": {"type": "string"}, "revision": {"type": "integer"}},
        ),
        tool(
            "stage_project_note",
            "Stage a complete note. Each claim requires evidence. The note publishes when the turn completes.",
            {
                "scope": {"type": "string"},
                "expected_revision": {"type": "integer"},
                "claims": {
                    "type": "array", "items": {
                        "type": "object", "properties": claim_fields,
                        "required": list(claim_fields), "additionalProperties": False,
                    },
                },
                "reason": {
                    "type": "string",
                    "description": "What changed and why, including how earlier evidence supported the prior conclusion.",
                },
            },
        ),
    ]


INSTRUCTIONS = (
    " For an ongoing account or project decision, search shared project notes by scope and read the matching note. "
    "Treat the note as a dated account. Compare its cited grounds with the relevant original evidence. "
    "When maintaining a note, keep unaffected claims and source URLs. If new evidence changes a claim, "
    "explain why, identify the earlier claim number in supersedes, and cite the new source beside the changed claim. "
    "Use each original upload's ../uploads/encoded-name#LN, prior file's revision_url#LN, "
    "saved artifact's ../artifacts/name?revision=N#LN, or a connected tool's evidence_url. "
    "To explain an earlier conclusion, use read_project_note_revision with the previous revision number. "
    "Use separate responses to read the note and sources before staging. Historical note revisions remain accessible."
)


def delivered_evidence(history, scope, revision):
    calls = {item["call_id"]: item.get("name") for item in history if item.get("type") == "function_call"}
    sources = set()
    read_note = False
    for item in history:
        if item.get("type") != "function_call_output":
            continue
        result = json.loads(item["output"])
        name = calls.get(item["call_id"])
        if (
            name == "read_project_note" and result.get("scope", "").casefold() == scope.casefold()
            and result.get("revision") == revision
        ):
            read_note = True
            for claim in result["claims"]:
                sources.update(claim["sources"])
        if result.get("lines"):
            for prefix in line_source_urls(name, result):
                sources.update(f"{prefix}#L{line['number']}" for line in result["lines"])
                if result.get("revision") is not None and len(result["lines"]) == result["total_lines"]:
                    sources.add(prefix)
        if name == "read_cited_evidence" and result.get("source_url") and "data" in result:
            sources.add(result["source_url"])
        if result.get("evidence_url") and "data" in result:
            sources.add(result["evidence_url"])
    return read_note, sources


def dispatch(call, history, store, stage_note):
    try:
        args = json.loads(call["arguments"])
        if not isinstance(args, dict) or (call.get("caller") or {}).get("type") == "program":
            raise ValueError("Call the project note tool directly with its JSON fields.")
        name = call["name"]
        if name == "search_project_notes" and args.keys() == {"query"}:
            return {"notes": store.search_project_notes(args["query"])}
        if name == "read_project_note" and args.keys() == {"scope"}:
            return store.read_project_note(args["scope"])
        if name == "read_project_note_revision" and args.keys() == {"scope", "revision"}:
            revision = args["revision"]
            if type(revision) is not int or not 1 <= revision < 2**63:
                raise ValueError("Specify an existing revision number.")
            note = store.read_project_note(args["scope"], revision)
            if not note["revision"]:
                raise ValueError("That note revision does not exist.")
            return note
        if name == "stage_project_note" and set(args) in (
            {"scope", "expected_revision", "claims"}, {"scope", "expected_revision", "claims", "reason"}
        ):
            scope = clean_scope(args["scope"])
            read_note, permitted = delivered_evidence(history, scope, args["expected_revision"])
            if not read_note:
                raise ValueError("Read this scope's current note revision in an earlier response before staging.")
            if not isinstance(args["claims"], list):
                raise ValueError("Supply a list of claims with their sources.")
            for claim in args["claims"]:
                if not isinstance(claim, dict) or not isinstance(claim.get("sources"), list):
                    raise ValueError("Supply source URLs for every claim.")
                if any(not isinstance(url, str) or url not in permitted for url in claim["sources"]):
                    raise ValueError("Read the cited source lines or revision first and use their exact URLs.")
            stage_note(**args)
            return {"staged": True, "scope": scope, "expected_revision": args["expected_revision"]}
        raise ValueError("Use the fields in the project note tool definition.")
    except (TypeError, ValueError, KeyError) as exc:
        return {"error": str(exc)}
