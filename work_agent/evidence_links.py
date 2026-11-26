"""Derived relationships between immutable files and published Markdown revisions."""

import json
from urllib.parse import quote

from .artifact_preview import FILE_CITATION, PRIOR_CITATION, file_citation, markdown_preview


PAGE_SIZE = 20


def initialize(connection, upload_bytes):
    connection.execute("""CREATE TABLE IF NOT EXISTS pending_upload_citations (
        source_task_id TEXT NOT NULL, source_name TEXT NOT NULL, report_task_id TEXT NOT NULL,
        report_name TEXT NOT NULL, report_revision INTEGER NOT NULL, line INTEGER NOT NULL,
        PRIMARY KEY(source_task_id, source_name, report_task_id, report_name, report_revision, line),
        FOREIGN KEY(source_task_id, source_name) REFERENCES uploads(task_id, name),
        FOREIGN KEY(report_task_id, report_name, report_revision) REFERENCES artifact_revisions(task_id, name, revision)
    ) WITHOUT ROWID""")
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='note_claim_citations'").fetchone():
        connection.execute("""CREATE TABLE note_claim_citations (
            source_base TEXT NOT NULL, note_id INTEGER NOT NULL, revision INTEGER NOT NULL, claim_index INTEGER NOT NULL,
            PRIMARY KEY(source_base, note_id, revision, claim_index),
            FOREIGN KEY(note_id, revision) REFERENCES project_note_revisions(note_id, revision)
        ) WITHOUT ROWID""")
        # Canonical source paths encode literal '#' in filenames; only the fragment names a row.
        select = """SELECT CASE WHEN instr(s.value, '#') THEN substr(s.value, 1, instr(s.value, '#')-1) ELSE s.value END,
            {row}.note_id, {row}.revision, c.key FROM {origin} json_each({row}.claims) c,
            json_each(c.value, '$.sources') s"""
        connection.execute("INSERT OR IGNORE INTO note_claim_citations " +
                           select.format(row="r", origin="project_note_revisions r,"))
        connection.execute("""CREATE TRIGGER index_note_evidence AFTER INSERT ON project_note_revisions BEGIN
            INSERT OR IGNORE INTO note_claim_citations """ + select.format(row="new", origin="") + "; END")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_citations'").fetchone():
        missing = connection.execute("SELECT DISTINCT source_task_id, source_name FROM pending_upload_citations").fetchall()
        for source_task_id, name in missing:
            resolve_pending(connection, upload_bytes, source_task_id, name)
        return
    # Zero identifies an upload (revisions start at 1) or a whole-file citation.
    connection.execute("""
        CREATE TABLE artifact_citations (
            source_task_id TEXT NOT NULL, source_name TEXT NOT NULL, source_revision INTEGER NOT NULL,
            report_task_id TEXT NOT NULL, report_name TEXT NOT NULL, report_revision INTEGER NOT NULL,
            line INTEGER NOT NULL,
            PRIMARY KEY(source_task_id, source_name, source_revision, report_task_id, report_name, report_revision, line),
            FOREIGN KEY(report_task_id, report_name, report_revision) REFERENCES artifact_revisions(task_id, name, revision)
        ) WITHOUT ROWID""")
    connection.execute("""
        CREATE TABLE evidence_reads (
            source_task_id TEXT NOT NULL, source_name TEXT NOT NULL, source_revision INTEGER NOT NULL,
            reading_task_id TEXT NOT NULL REFERENCES tasks(id),
            PRIMARY KEY(source_task_id, source_name, source_revision, reading_task_id)
        ) WITHOUT ROWID""")
    connection.execute("""CREATE INDEX IF NOT EXISTS run_response_lookup ON run_steps(task_id, turn_id, id)
        WHERE json_extract(payload, '$.type')='response'""")
    for row in connection.execute("SELECT task_id, name, revision, content FROM artifact_revisions WHERE name LIKE '%.md'"):
        index_report(connection, upload_bytes, *row)
    for row in connection.execute("""SELECT task_id, turn_id, payload, id FROM run_steps
            WHERE json_extract(payload, '$.type')='function_call_output' ORDER BY id"""):
        record_read(connection, row[0], row[1], json.loads(row[2]), row[3])


def _source_length(connection, upload_bytes, target):
    """Return -1 for an unknown file, None for a temporarily missing legacy original."""
    task_id, name, revision = target
    if revision:
        row = connection.execute("""SELECT content FROM artifact_revisions
            WHERE task_id=? AND name=? AND revision=?""", target).fetchone()
        data = row[0] if row else None
    else:
        last_page = connection.execute("""SELECT end_line FROM pdf_upload_pages
            WHERE task_id=? AND name=? ORDER BY number DESC LIMIT 1""", (task_id, name)).fetchone()
        if last_page:
            return last_page[0]
        office = connection.execute("SELECT text FROM office_upload_text WHERE task_id=? AND name=?", (task_id,name)).fetchone()
        if office:
            return len(office[0].splitlines())
        row = connection.execute("SELECT content, position FROM uploads WHERE task_id=? AND name=?", (task_id, name)).fetchone()
        try:
            data = upload_bytes(task_id, row) if row else None
        except FileNotFoundError:
            return None
    return len(data.decode("utf-8").splitlines()) if data is not None else -1


def physical_pdf_page(connection, task, name, line):
    """Return the physical page and text availability at an upload's global line."""
    return connection.execute("""SELECT number, text!='' AS has_text FROM pdf_upload_pages
        WHERE task_id=? AND name=? AND ? BETWEEN start_line AND end_line""", (task, name, line)).fetchone()


def index_report(connection, upload_bytes, task_id, name, revision, content):
    if not name.endswith(".md"):
        return
    citations = set()
    pending = set()
    lengths = {}
    for block in markdown_preview.parse(content.decode("utf-8")):
        for token in block.children or ():
            if token.type != "link_open":
                continue
            href = token.attrGet("href")
            direct = file_citation(href)
            prior = PRIOR_CITATION.fullmatch(href)
            if direct:
                source_task, source_name, source_revision, source_line = direct
                target = (source_task or task_id, source_name, source_revision or 0)
                line = source_line or 0
                page = FILE_CITATION.fullmatch(href)["page"]
            elif prior and int(prior["reference"]) <= 2**63 - 1:
                row = connection.execute("""SELECT source_task_id, name, COALESCE(revision, 0) FROM prior_artifacts
                    WHERE task_id=? AND reference_id=?""", (task_id, int(prior["reference"]))).fetchone()
                if row is None:
                    continue
                target = tuple(row)
                line = int(prior["line"] or 0)
                page = prior["page"]
            else:
                continue
            physical = physical_pdf_page(connection, *target[:2], line) if line and not target[2] else None
            if physical and not physical["has_text"]:
                continue
            if page and (not physical or physical["number"] != int(page)):
                continue
            if target not in lengths:
                lengths[target] = _source_length(connection, upload_bytes, target)
            if lengths[target] is None:
                pending.add((*target[:2], task_id, name, revision, line))
            elif line <= lengths[target]:
                citations.add((*target, task_id, name, revision, line))
    connection.executemany("INSERT INTO artifact_citations VALUES (?, ?, ?, ?, ?, ?, ?)", citations)
    connection.executemany("INSERT OR IGNORE INTO pending_upload_citations VALUES (?, ?, ?, ?, ?, ?)", pending)


def resolve_pending(connection, upload_bytes, task_id, name):
    lines = _source_length(connection, upload_bytes, (task_id, name, 0))
    if lines is None:
        return
    connection.execute("""INSERT OR IGNORE INTO artifact_citations
        SELECT source_task_id, source_name, 0, report_task_id, report_name, report_revision, line
        FROM pending_upload_citations WHERE source_task_id=? AND source_name=? AND line<=?""", (task_id, name, lines))
    connection.execute("DELETE FROM pending_upload_citations WHERE source_task_id=? AND source_name=?", (task_id, name))


def record_read(connection, task_id, turn_id, step, step_id):
    if step.get("type") != "function_call_output":
        return
    try:
        result = json.loads(step["output"])
    except (KeyError, TypeError, ValueError):
        return
    if not isinstance(result, dict) or not result.get("lines") or not isinstance(result.get("file_id"), str):
        return
    # Responses store the provider's full output array; match the receipt to its
    # exact call in the preceding response, including for multi-call hosted programs.
    envelope = connection.execute("""SELECT json_extract(payload, '$.response.output') FROM run_steps
        WHERE task_id=? AND turn_id IS ? AND id<? AND json_extract(payload, '$.type')='response'
        ORDER BY id DESC LIMIT 1""", (task_id, turn_id, step_id)).fetchone()
    if not envelope or not envelope[0]:
        return
    call = next((item for item in json.loads(envelope[0]) if item.get("type") == "function_call"
                 and item.get("call_id") == step.get("call_id")), None)
    if call is None:
        return
    try:
        args = json.loads(call["arguments"])
    except (KeyError, TypeError, ValueError):
        return
    if not isinstance(args, dict):
        return
    name = result["file_id"]
    if call.get("name") == "read_file" and args.get("file_id") == name:
        target = connection.execute("SELECT task_id, name, 0 FROM uploads WHERE task_id=? AND name=?", (task_id, name)).fetchone()
    elif call.get("name") == "read_prior_artifact" and type(args.get("reference_id")) is int:
        target = connection.execute("""SELECT source_task_id, name, COALESCE(revision, 0) FROM prior_artifacts
            WHERE task_id=? AND reference_id=? AND name=?""", (task_id, args["reference_id"], name)).fetchone()
    elif call.get("name") == "read_cited_evidence":
        requested = args.get("source_url")
        if not isinstance(requested, str) or requested.partition("#")[0].partition("?page=")[0] != result.get("source_url"):
            return
        citation = file_citation(requested)
        if not citation or citation[:3] != (result.get("source_task_id"), name, result.get("revision")):
            return
        source_task, source_name, source_revision, _ = citation
        if source_revision is None:
            target = connection.execute("SELECT task_id, name, 0 FROM uploads WHERE task_id=? AND name=?",
                                        (source_task, source_name)).fetchone()
        else:
            target = connection.execute("SELECT task_id, name, revision FROM artifact_revisions WHERE task_id=? AND name=? AND revision=?",
                                        (source_task, source_name, source_revision)).fetchone()
    elif call.get("name") == "read_saved_artifact" and args.get("name") == name:
        target = connection.execute("""SELECT ?, name, revision FROM turn_artifacts
            WHERE turn_id=? AND name=? AND revision=?""", (task_id, turn_id, name, result.get("revision"))).fetchone()
    else:
        return
    if target:
        connection.execute("INSERT OR IGNORE INTO evidence_reads VALUES (?, ?, ?, ?)", (*target, task_id))


def _line_numbers(encoded):
    return [n or None for n in sorted(map(int, encoded.split(",")))]


def _indirect_uses(connection, target, offset):
    # Exactly two citation edges. Each node is an immutable file revision.
    # A source appearing again anywhere in the path closes a cycle and is excluded.
    rows = connection.execute("""
        WITH direct AS (
          SELECT report_task_id, report_name, report_revision, group_concat(line) AS source_lines
          FROM artifact_citations
          WHERE (source_task_id, source_name, source_revision)=(?,?,?)
            AND (report_task_id, report_name, report_revision)!=(source_task_id, source_name, source_revision)
          GROUP BY report_task_id, report_name, report_revision
        )
        SELECT d.report_task_id, d.report_name, d.report_revision, d.source_lines,
               c.report_task_id, c.report_name, c.report_revision, group_concat(c.line),
               (SELECT MAX(revision) FROM artifact_revisions WHERE task_id=c.report_task_id AND name=c.report_name)
        FROM direct d JOIN artifact_citations c ON
            (c.source_task_id, c.source_name, c.source_revision)=(d.report_task_id, d.report_name, d.report_revision)
        WHERE (c.report_task_id, c.report_name, c.report_revision)!=(?,?,?)
          AND (c.report_task_id, c.report_name, c.report_revision)!=(d.report_task_id, d.report_name, d.report_revision)
        GROUP BY d.report_task_id, d.report_name, d.report_revision, c.report_task_id, c.report_name, c.report_revision
        ORDER BY d.report_task_id, d.report_name, d.report_revision DESC, c.report_task_id, c.report_name, c.report_revision DESC
        LIMIT ? OFFSET ?""", (*target, *target, PAGE_SIZE + 1, offset)).fetchall()
    return [{"source_task_id": row[4], "name": row[5], "revision": row[6], "current": row[6] == row[8],
             "lines": _line_numbers(row[7]),
             "via": {"source_task_id": row[0], "name": row[1], "revision": row[2], "lines": _line_numbers(row[3])}}
            for row in rows]


def uses(connection, task_id, name, revision, offset=0):
    if type(offset) is not int or not 0 <= offset < 2**63 - PAGE_SIZE:
        raise ValueError("Provide a nonnegative offset.")
    target = (task_id, name, revision or 0)
    # Page by report revision, not by the number of source lines it cites.
    rows = connection.execute("""SELECT c.report_task_id, c.report_name, c.report_revision,
          (SELECT MAX(revision) FROM artifact_revisions r WHERE r.task_id=c.report_task_id AND r.name=c.report_name),
          group_concat(c.line)
        FROM artifact_citations c
        WHERE c.source_task_id=? AND c.source_name=? AND c.source_revision=?
        GROUP BY c.report_task_id, c.report_name, c.report_revision
        ORDER BY c.report_task_id, c.report_name, c.report_revision DESC LIMIT ? OFFSET ?""",
        (*target, PAGE_SIZE + 1, offset)).fetchall()
    citing = [{"source_task_id": task, "name": report, "revision": rev, "current": rev == current,
               "lines": _line_numbers(lines)}
              for task, report, rev, current, lines in rows[:PAGE_SIZE]]
    # A pin is a selection. Only a successful nonempty read receipt records a read.
    readers = connection.execute("""SELECT reader, max(pinned), max(was_read) FROM (
          SELECT task_id AS reader, 1 AS pinned, 0 AS was_read FROM prior_artifacts
              WHERE source_task_id=? AND name=? AND revision IS ?
          UNION ALL SELECT reading_task_id, 0, 1 FROM evidence_reads
              WHERE source_task_id=? AND source_name=? AND source_revision=?
        ) GROUP BY reader ORDER BY reader LIMIT ? OFFSET ?""",
        (task_id, name, revision, *target, PAGE_SIZE + 1, offset)).fetchall()
    collection = "uploads" if revision is None else "artifacts"
    source_base = f"/tasks/{task_id}/{collection}/{quote(name, safe='')}"
    if revision is not None:
        source_base += f"?revision={revision}"
    notes = connection.execute("""SELECT n.id, n.scope, r.revision, r.reason,
            json_extract(r.claims, '$[' || c.claim_index || ']'),
            (SELECT MAX(revision) FROM project_note_revisions x WHERE x.note_id=n.id), c.claim_index
        FROM note_claim_citations c JOIN project_notes n ON n.id=c.note_id
        JOIN project_note_revisions r ON r.note_id=n.id AND r.revision=c.revision
        WHERE c.source_base IN (SELECT ? UNION ALL SELECT ?||'?page='||number FROM pdf_upload_pages
                WHERE task_id=? AND name=? AND ? IS NULL)
        GROUP BY n.id,r.revision,c.claim_index ORDER BY n.id, r.revision DESC, c.claim_index LIMIT ? OFFSET ?""",
        (source_base, source_base, task_id, name, revision, PAGE_SIZE + 1, offset)).fetchall()
    claims = [{"note_id": n[0], "scope": n[1], "revision": n[2], "reason": n[3],
               "claim": json.loads(n[4]), "current_revision": n[5],
               "note_url": f"/notes/{n[0]}?revision={n[2]}#claim-{n[6]+1}"} for n in notes[:PAGE_SIZE]]
    indirect = _indirect_uses(connection, target, offset)
    for row in [*citing, *indirect[:PAGE_SIZE], *(item["via"] for item in indirect[:PAGE_SIZE])]:
        row["revision_url"] = f"/tasks/{row['source_task_id']}/artifacts/{quote(row['name'], safe='')}?revision={row['revision']}"
    return {"citing_revisions": citing, "indirect_review_candidates": indirect[:PAGE_SIZE], "note_claims": claims,
            "related_assignments": [{"task_id": r[0], "pinned": bool(r[1]), "read": bool(r[2])} for r in readers[:PAGE_SIZE]],
            "next_offset": offset + PAGE_SIZE if max(len(rows), len(readers), len(notes), len(indirect)) > PAGE_SIZE else None}
