"""Explicit, scoped relationships between exact lines in immutable evidence."""

import json
import re
from urllib.parse import quote


RELATIONSHIPS = ("supersedes", "qualifies", "contradicts")
SNAPSHOT_URL = re.compile(r"(?:\.\.|/tasks/[0-9a-f]{32})/evidence/[0-9a-f]{32}(?:#.*)?")
PAGE_SIZE = 20
SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_changes (
    id INTEGER PRIMARY KEY,
    turn_id INTEGER NOT NULL REFERENCES turns(id),
    new_doc TEXT NOT NULL, new_line INTEGER NOT NULL,
    earlier_doc TEXT NOT NULL, earlier_line INTEGER NOT NULL,
    relationship TEXT NOT NULL CHECK(relationship IN ('supersedes','qualifies','contradicts')),
    scope TEXT NOT NULL, reason TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0,
    UNIQUE(turn_id, new_doc, new_line, earlier_doc, earlier_line, relationship, scope)
);
CREATE INDEX IF NOT EXISTS evidence_changes_new ON evidence_changes(new_doc,new_line) WHERE published=1;
CREATE INDEX IF NOT EXISTS evidence_changes_earlier ON evidence_changes(earlier_doc,earlier_line) WHERE published=1;
"""

TOOL_PARAMETERS = {
    "new_source": {"type": "string", "description": "Exact line URL for the newly considered source."},
    "earlier_source": {"type": "string", "description": "Exact line URL for the earlier source."},
    "relationship": {"type": "string", "enum": list(RELATIONSHIPS)},
    "scope": {"type": "string", "description": "Where and when this relationship applies."},
    "reason": {"type": "string", "description": "Explain what the two passages establish and any uncertainty."},
}

INSTRUCTIONS = (
    " When two numbered file passages already read materially change the evidence, use stage_evidence_change to record the "
    "source relationship, its applicable scope and the reason. Use supersedes only for an established "
    "replacement, qualifies for a narrower condition or exception, and contradicts for unresolved conflict. "
    "A later date alone does not establish supersession. A document can have different rules in different "
    "sections. Read results can contain evidence_changes from completed assignments. Check their scope "
    "and revisit the cited passages when deciding whether to revise, reaffirm or leave a conclusion open. "
    "On current project notes, claim_number identifies the claim whose cited line changed. "
    "If the summary is truncated, read individual claim citations for the remaining relationships. "
    "Open either canonical source URL with read_cited_evidence to verify the exact bytes. "
    "Connected tools return whole snapshots. Cite their exact evidence URLs together in the finding, "
    "report or project note when explaining a change between snapshots."
)


class EvidenceChangeStore:
    def stage_evidence_change(self, task_id, turn_id, new_source, earlier_source, relationship, scope, reason):
        if relationship not in RELATIONSHIPS:
            raise ValueError("Choose supersedes, qualifies, or contradicts.")
        if not isinstance(scope, str) or not 1 <= len(scope.strip()) <= 300:
            raise ValueError("Specify the affected scope in up to 300 characters.")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
            raise ValueError("Explain how the evidence changed in up to 1000 characters.")
        new = self.resolve_note_source(task_id, new_source)
        earlier = self.resolve_note_source(task_id, earlier_source)
        if new == earlier or "#L" not in new or "#L" not in earlier:
            raise ValueError("Choose two distinct, immutable source lines.")
        new_doc, new_line = new.rsplit("#L", 1)
        earlier_doc, earlier_line = earlier.rsplit("#L", 1)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._running_turn(connection, task_id, turn_id)
            connection.execute(
                """INSERT INTO evidence_changes
                   (turn_id,new_doc,new_line,earlier_doc,earlier_line,relationship,scope,reason)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(turn_id,new_doc,new_line,earlier_doc,earlier_line,relationship,scope)
                   DO UPDATE SET reason=excluded.reason""",
                (turn_id, new_doc, int(new_line), earlier_doc, int(earlier_line),
                 relationship, scope.strip(), reason.strip()),
            )
        return {"staged_evidence_change": True, "relationship": relationship, "scope": scope.strip(),
                "new_source": new, "earlier_source": earlier, "reason": reason.strip()}

    def changes_for_note_claims(self, claims):
        citations = []
        for claim_number, claim in enumerate(claims, 1):
            for source in claim["sources"]:
                doc, anchor, line = source.rpartition("#L")
                if anchor and line.isascii() and line.isdigit():
                    citations.append((claim_number, doc, int(line)))
        if not citations:
            return {}
        # A qualifying or replacing passage changes its predecessor. An unresolved
        # contradiction is relevant to either endpoint. Group repeated endpoints
        # within a claim and show one result per affected claim before extra results.
        with self._connection() as connection:
            rows = connection.execute(
                """WITH sources(claim, doc, line) AS (
                       SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),json_extract(value,'$[2]')
                       FROM json_each(?)
                   ), endpoints AS (
                       SELECT s.claim,c.id,s.doc||'#L'||s.line AS cited_source
                       FROM sources s CROSS JOIN evidence_changes c
                       WHERE c.published=1 AND c.earlier_doc=s.doc AND c.earlier_line=s.line
                       UNION ALL
                       SELECT s.claim,c.id,s.doc||'#L'||s.line AS cited_source
                       FROM sources s CROSS JOIN evidence_changes c
                       WHERE c.published=1 AND c.relationship='contradicts' AND c.new_doc=s.doc AND c.new_line=s.line
                   ), hits AS (
                       SELECT claim,id,min(cited_source) AS cited_source FROM endpoints GROUP BY claim,id
                   ), ranked AS (
                       SELECT *,row_number() OVER (PARTITION BY claim ORDER BY id DESC) AS position FROM hits
                   )
                   SELECT r.claim AS claim_number,r.cited_source,c.new_doc||'#L'||c.new_line AS new_source,
                          c.earlier_doc||'#L'||c.earlier_line AS earlier_source,c.relationship,c.scope,c.reason,
                          '/tasks/'||t.task_id AS assignment_url
                   FROM ranked r JOIN evidence_changes c ON r.id=c.id JOIN turns t ON t.id=c.turn_id
                   ORDER BY r.position,r.claim LIMIT ?""",
                (json.dumps(citations), PAGE_SIZE + 1),
            ).fetchall()
        if not rows:
            return {}
        return {"changes": [dict(row) for row in rows[:PAGE_SIZE]], "truncated": len(rows) > PAGE_SIZE}

    def evidence_changes(self, source_task_id, name, revision, offset=0, *, start_line=1, end_line=None):
        if type(offset) is not int or not 0 <= offset <= 2**63 - 1:
            raise ValueError("Use a nonnegative offset.")
        kind = "uploads" if revision is None else "artifacts"
        source = f"/tasks/{source_task_id}/{kind}/{quote(name, safe='')}"
        if revision is not None:
            source += f"?revision={revision}"
        last_line = end_line if end_line is not None else 2**63 - 1
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT c.new_doc||'#L'||c.new_line AS new_source,
                          c.earlier_doc||'#L'||c.earlier_line AS earlier_source,
                          c.relationship,c.scope,c.reason,'/tasks/'||t.task_id AS assignment_url
                   FROM evidence_changes c JOIN turns t ON t.id=c.turn_id
                   WHERE c.published=1 AND ((c.new_doc=? AND c.new_line BETWEEN ? AND ?)
                      OR (c.earlier_doc=? AND c.earlier_line BETWEEN ? AND ?))
                   ORDER BY c.id DESC LIMIT ? OFFSET ?""",
                (source, start_line, last_line, source, start_line, last_line, PAGE_SIZE + 1, offset),
            ).fetchall()
        return {"changes": [dict(row) for row in rows[:PAGE_SIZE]],
                "next_offset": offset + PAGE_SIZE if len(rows) > PAGE_SIZE else None}


class AssignmentEvidenceChanges:
    def __init__(self, store, task_id, turn_id):
        self.store = store
        self.task_id = task_id
        self.turn_id = turn_id
        self._read_lines = set()

    @property
    def has_two_lines(self):
        return len(self._read_lines) == 2

    def stage(self, arguments, read_lines):
        if arguments.keys() != TOOL_PARAMETERS.keys():
            raise ValueError("Supply both source lines, their relationship, scope and reason.")
        sources = (arguments["new_source"], arguments["earlier_source"])
        if any(isinstance(url, str) and SNAPSHOT_URL.fullmatch(url) for url in sources):
            raise ValueError("Connected snapshot URLs identify whole tool results. Cite their exact URLs directly "
                             "in the finding, report or project note and explain the change there.")
        if not all(isinstance(url, str) and url in read_lines for url in sources):
            raise ValueError("Read both exact source lines in an earlier response before linking them.")
        if any(not read_lines[url][1].strip() for url in sources):
            raise ValueError("Choose source lines containing the passages that changed.")
        return self.store.stage_evidence_change(self.task_id, self.turn_id, **arguments)

    def annotate(self, tool, result):
        if tool == "read_project_note" and result.get("claims"):
            changes = self.store.changes_for_note_claims(result["claims"])
            return {**result, "evidence_changes": changes} if changes else result
        if tool not in {"read_file", "read_prior_artifact", "read_saved_artifact", "read_cited_evidence"}:
            return result
        if not result.get("lines"):
            return result
        # A pinned URL and a canonical URL can refer to the very same line.
        # Retain at most two identities to decide when the model can use the tool.
        if not self.has_two_lines:
            file_identity = (result.get("source_task_id", self.task_id), result["file_id"], result.get("revision"))
            for line in result["lines"]:
                if line["text"].strip():
                    self._read_lines.add((*file_identity, line["number"]))
                    if self.has_two_lines:
                        break
        page = self.store.evidence_changes(
            result.get("source_task_id", self.task_id), result["file_id"], result.get("revision"),
            start_line=result["lines"][0]["number"], end_line=result["lines"][-1]["number"],
        )
        if page["changes"]:
            return {**result, "evidence_changes": page}
        return result
