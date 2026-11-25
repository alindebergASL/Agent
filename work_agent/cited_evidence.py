"""Follow an immutable source link returned by workspace knowledge tools."""

from urllib.parse import quote, unquote

from .project_notes import LOCAL_SOURCE


def line_source_urls(tool, result):
    if tool == "read_file":
        return ["../uploads/" + quote(result["file_id"], safe="")]
    if tool == "read_prior_artifact":
        return [result["revision_url"]]
    if tool == "read_cited_evidence":
        return [url for url in (result["source_url"], result.get("revision_url")) if url]
    if tool == "read_saved_artifact":
        return [f"../artifacts/{quote(result['file_id'], safe='')}?revision={result['revision']}"]
    return []


def remember_lines(receipts, tool, result):
    if result.get("lines"):
        for url in line_source_urls(tool, result):
            for line in result["lines"]:
                receipts[f"{url}#L{line['number']}"] = (result["file_id"], line["text"])


def disclosed_sources(tool, result):
    """Accept only named fields of application-owned results, never URLs found in source text."""
    links = set()
    if tool in {"read_project_note", "read_project_note_revision"}:
        for claim in result.get("claims", []):
            links.update(claim["sources"])
    if tool == "find_evidence_uses":
        for entry in result.get("note_claims", []):
            links.update(entry["claim"]["sources"])
        for entry in result.get("citing_revisions", []):
            if entry.get("revision_url"):
                links.add(entry["revision_url"])
        for entry in result.get("indirect_review_candidates", []):
            links.update((entry["revision_url"], entry["via"]["revision_url"]))
    if tool in {"read_project_note", "find_evidence_uses", "read_file", "read_saved_artifact",
                "read_prior_artifact", "read_cited_evidence"}:
        changes = result.get("evidence_changes", [])
        for row in changes.get("changes", []) if isinstance(changes, dict) else changes:
            links.update((row["new_source"], row["earlier_source"]))
    return links


def read_link(arguments, disclosed, store, current_task_id, pin_source, read_page):
    if arguments.keys() != {"source_url", "start_line", "line_count"}:
        raise ValueError("Provide a cited source URL, a starting line and a line count.")
    url = arguments["source_url"]
    if not isinstance(url, str) or url not in disclosed:
        raise ValueError("Choose an exact workspace URL from a note, source relationship or evidence-usage result already returned.")
    match = LOCAL_SOURCE.fullmatch(url)
    if not match:
        raise ValueError("This link does not identify an immutable workspace source.")
    task_id, kind, encoded, revision, anchor = match.groups()
    base = url.partition("#")[0]
    name = unquote(encoded)
    if kind == "evidence":
        snapshot = store.source_evidence(task_id, name)
        if snapshot is None:
            raise ValueError("That connected evidence snapshot is unavailable.")
        return {"source_url": base, "source_task_id": task_id, "data": snapshot["data"],
                "source": snapshot.get("source"), "tool": snapshot.get("tool")}
    if kind == "artifacts" and revision is None:
        raise ValueError("A generated file link must specify its exact revision.")
    rev = int(revision) if revision else None
    if kind == "uploads":
        content = store.read_upload(task_id, name)
    else:
        artifact = store.artifact_revision(task_id, name, rev)
        content = artifact["content"] if artifact else None
    start = arguments["start_line"]
    if type(start) is int and start == 0:
        start = int(anchor or 1)
    page = read_page(name, {"source_task_id": task_id, "source_url": base, "revision": rev},
                     content, start, arguments["line_count"])
    if current_task_id and task_id != current_task_id and pin_source is not None:
        ref = pin_source(task_id, name, rev)
        page.update(reference_id=ref["reference_id"], revision_url=f"../prior-artifacts/{ref['reference_id']}")
    elif task_id == current_task_id:
        page["revision_url"] = f"../{kind}/{encoded}" + (f"?revision={revision}" if revision else "")
    return page
