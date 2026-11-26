"""Scoped shared notes. Revisions and their source URLs are immutable."""

import json
import re
from urllib.parse import quote, unquote

from .artifact_preview import PRIOR_CITATION
from .evidence_links import physical_pdf_page

SCHEMA = """
CREATE TABLE IF NOT EXISTS project_notes (
    id INTEGER PRIMARY KEY, scope TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS project_note_revisions (
    note_id INTEGER NOT NULL REFERENCES project_notes(id),
    revision INTEGER NOT NULL, author TEXT NOT NULL, task_id TEXT REFERENCES tasks(id),
    claims TEXT NOT NULL, reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY(note_id, revision)
);
CREATE TABLE IF NOT EXISTS staged_project_notes (
    turn_id INTEGER NOT NULL REFERENCES turns(id), scope TEXT NOT NULL COLLATE NOCASE,
    expected_revision INTEGER NOT NULL, claims TEXT NOT NULL, reason TEXT NOT NULL,
    PRIMARY KEY(turn_id, scope)
);
"""

SOURCE_PATH = (r"(uploads|artifacts|evidence)/([^/#?]+)"
               r"(?:\?revision=([1-9][0-9]{0,17})|\?page=([1-9][0-9]{0,5}))?(?:#L([1-9][0-9]{0,8}))?")
LOCAL_SOURCE = re.compile(r"/tasks/([0-9a-f]{32})/" + SOURCE_PATH)
LOCAL_ALIAS = re.compile(r"\.\./" + SOURCE_PATH)
MAX_CLAIMS = 32


class NoteConflict(ValueError):
    pass


def clean_scope(scope):
    if not isinstance(scope, str) or not 1 <= len(scope.strip()) <= 160:
        raise ValueError("Enter an account or project scope of 1 to 160 characters.")
    return scope.strip()


def note_revision(connection, scope, revision=None):
    query = """SELECT n.id, n.scope, r.revision, r.claims, r.author, r.reason, r.task_id, r.created_at
               FROM project_notes n JOIN project_note_revisions r ON n.id=r.note_id
               WHERE n.scope=?"""
    values = [scope]
    if revision is not None:
        query += " AND r.revision=?"
        values.append(revision)
    row = connection.execute(query + " ORDER BY r.revision DESC LIMIT 1", values).fetchone()
    if not row:
        return {"scope": scope, "revision": 0, "claims": []}
    note = dict(row)
    note["claims"] = json.loads(note["claims"])
    note["previous_revision"] = note["revision"] - 1 or None
    return note


def write_revision(connection, scope, expected, claims_json, reason, author, task_id):
    note = note_revision(connection, scope)
    if note["revision"] != expected:
        raise NoteConflict(f'The note for {scope} changed. Read its current revision and retry.')
    if expected == 0:
        note_id = connection.execute("INSERT INTO project_notes(scope) VALUES (?)", (scope,)).lastrowid
    else:
        note_id = note["id"]
    claims = json.loads(claims_json)
    for claim in claims:
        predecessor = claim.get("supersedes")
        if predecessor is not None and not 1 <= predecessor <= len(note["claims"]):
            raise ValueError("The superseded claim does not exist in the previous revision.")
    connection.execute(
        """INSERT INTO project_note_revisions(note_id,revision,claims,reason,author,task_id)
           VALUES (?,?,?,?,?,?)""", (note_id, expected + 1, claims_json, reason, author, task_id)
    )


def publish_notes(connection, task_id, turn_id):
    rows = connection.execute("SELECT * FROM staged_project_notes WHERE turn_id=?", (turn_id,)).fetchall()
    for row in rows:
        write_revision(connection, row["scope"], row["expected_revision"], row["claims"],
                       row["reason"], "agent", task_id)


class ProjectNoteStore:
    def read_project_note(self, scope, revision=None):
        with self._connection() as connection:
            return note_revision(connection, clean_scope(scope), revision)

    def project_note_by_id(self, note_id, revision=None):
        with self._connection() as connection:
            row = connection.execute("SELECT scope FROM project_notes WHERE id=?", (note_id,)).fetchone()
            if row is not None:
                note = note_revision(connection, row["scope"], revision)
                return note if note["revision"] else None

    def search_project_notes(self, query):
        if not isinstance(query, str) or len(query) > 160:
            raise ValueError("Use a project or account name of up to 160 characters.")
        phrase = query.strip()
        terms = list(dict.fromkeys(re.findall(r"[^\W_]+", phrase)))[:24]
        # Only scope metadata participates in discovery. Claims arrive after an explicit read.
        with self._connection() as connection:
            score = " + ".join("(instr(lower(n.scope),lower(?)) > 0)" for _ in terms) or "0"
            query_sql = f"""SELECT n.id,n.scope,MAX(r.revision) AS revision,
                       instr(lower(n.scope),lower(?)) > 0 AS exact, {score} AS matched
                   FROM project_notes n JOIN project_note_revisions r ON r.note_id=n.id
                   GROUP BY n.id HAVING exact OR matched > 0
                   ORDER BY exact DESC, matched DESC, n.scope LIMIT 100"""
            rows = connection.execute(query_sql, [phrase, *terms])
            return [{key: row[key] for key in ("id", "scope", "revision")} for row in rows]

    def resolve_note_source(self, task_id, source):
        if not isinstance(source, str) or len(source) > 2048:
            raise ValueError("Provide a source URL for each claim.")
        pin = PRIOR_CITATION.fullmatch(source)
        if task_id and pin:
            ref = self.prior_artifact(task_id, int(pin["reference"]))
            if ref is None:
                raise ValueError("The cited earlier file was not pinned to this assignment.")
            kind = "uploads" if ref["revision"] is None else "artifacts"
            url = f"/tasks/{ref['source_task_id']}/{kind}/{quote(ref['name'], safe='')}"
            if ref["revision"] is not None:
                url += f"?revision={ref['revision']}"
            if pin["page"]:
                if ref["revision"] is not None:
                    raise ValueError("Physical pages belong to original PDF uploads.")
                url += f"?page={pin['page']}"
            source = url + (f"#L{pin['line']}" if pin["line"] else "")
        elif task_id and LOCAL_ALIAS.fullmatch(source):
            source = f"/tasks/{task_id}/" + source[3:]
        match = LOCAL_SOURCE.fullmatch(source)
        if not match:
            raise ValueError("Cite an exact original upload line, saved file revision, or connected evidence snapshot.")
        source_task, kind, encoded, revision, page, line = match.groups()
        name = unquote(encoded)
        if kind == "evidence":
            if revision or line or page or self.source_evidence(source_task, name) is None:
                raise ValueError("Unknown connected evidence snapshot.")
            return f"/tasks/{source_task}/evidence/{name}"
        if kind == "uploads":
            if revision or not line:
                raise ValueError("Cite an original upload at a specific line, for example #L2.")
            with self._connection() as db:
                source_page = physical_pdf_page(db, source_task, name, int(line))
            if source_page:
                if not source_page["has_text"] or (page and int(page) != source_page["number"]):
                    raise ValueError("The cited PDF page does not contain this extracted source line.")
                return f"/tasks/{source_task}/uploads/{quote(name, safe='')}?page={source_page['number']}#L{line}"
            if page:
                raise ValueError("The cited PDF page or source line does not exist.")
            content = self.read_upload_text(source_task, name)
        else:
            if not revision:
                raise ValueError("Pin a specific generated file revision.")
            artifact = self.artifact_revision(source_task, name, int(revision))
            content = artifact["content"] if artifact else None
        if content is None or (line and int(line) > len(content.decode("utf-8").splitlines())):
            raise ValueError("The cited file or line does not exist.")
        url = f"/tasks/{source_task}/{kind}/{quote(name, safe='')}"
        if revision:
            url += f"?revision={revision}"
        return url + (f"#L{line}" if line else "")

    def _note_content(self, task_id, expected_revision, claims, reason):
        if type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1:
            raise ValueError("Specify the revision you read.")
        if not isinstance(claims, list) or not 1 <= len(claims) <= MAX_CLAIMS:
            raise ValueError(f"Supply between 1 and {MAX_CLAIMS} claims.")
        if not isinstance(reason, str) or len(reason) > 1500 or (expected_revision and not reason.strip()):
            raise ValueError("Explain the evidence or correction that changed this note in up to 1500 characters.")
        result = []
        resolved = {}
        for claim in claims:
            if not isinstance(claim, dict) or set(claim) not in ({"text", "sources"}, {"text", "sources", "supersedes"}):
                raise ValueError("Each claim needs text and a list of evidence URLs.")
            text, sources = claim["text"], claim["sources"]
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                raise ValueError("Keep each claim between 1 and 2000 characters.")
            if not isinstance(sources, list) or not 1 <= len(sources) <= 12:
                raise ValueError("Attach one to twelve evidence URLs to each claim.")
            for url in sources:
                if not isinstance(url, str):
                    raise ValueError("Each evidence URL must be a string.")
                if url not in resolved:
                    resolved[url] = self.resolve_note_source(task_id, url)
            urls = dict.fromkeys(resolved[url] for url in sources)
            data = {"text": text.strip(), "sources": list(urls)}
            predecessor = claim.get("supersedes")
            if predecessor is not None:
                if type(predecessor) is not int or not expected_revision or not 1 <= predecessor <= MAX_CLAIMS:
                    raise ValueError("Choose a 1-based claim number in the previous revision.")
                data["supersedes"] = predecessor
            result.append(data)
        return json.dumps(result, ensure_ascii=False)

    def stage_project_note(self, task_id, turn_id, scope, expected_revision, claims, reason=""):
        scope = clean_scope(scope)
        content = self._note_content(task_id, expected_revision, claims, reason)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._running_turn(connection, task_id, turn_id)
            current = note_revision(connection, scope)
            if any(claim.get("supersedes", 0) > len(current["claims"]) for claim in json.loads(content)):
                raise ValueError("The superseded claim does not exist in the previous revision.")
            if current["revision"] != expected_revision:
                raise NoteConflict("The note changed. Read it again before staging.")
            connection.execute(
                """INSERT INTO staged_project_notes(turn_id, scope, expected_revision, claims, reason)
                   VALUES (?,?,?,?,?) ON CONFLICT(turn_id,scope) DO UPDATE SET
                   expected_revision=excluded.expected_revision, claims=excluded.claims, reason=excluded.reason""",
                (turn_id, scope, expected_revision, content, reason.strip()),
            )

    def edit_project_note(self, note_id, expected_revision, claims, reason):
        note = self.project_note_by_id(note_id)
        if note is None:
            raise ValueError("Project note not found.")
        content = self._note_content(None, expected_revision, claims, reason)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            write_revision(connection, note["scope"], expected_revision, content, reason.strip(), "user", None)
