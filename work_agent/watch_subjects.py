"""Optional workspace item being watched, with a durable starting revision."""

import re
from urllib.parse import quote


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS watch_subjects (
        watch_id INTEGER PRIMARY KEY REFERENCES watches(id),
        note_id INTEGER, source_task TEXT, source_name TEXT, revision INTEGER NOT NULL,
        CHECK ((note_id IS NOT NULL AND source_task IS NULL AND source_name IS NULL)
            OR (note_id IS NULL AND source_task IS NOT NULL AND source_name IS NOT NULL)),
        FOREIGN KEY(note_id,revision) REFERENCES project_note_revisions(note_id,revision),
        FOREIGN KEY(source_task,source_name,revision) REFERENCES artifact_revisions(task_id,name,revision)
    )""")


def _positive(value):
    if not re.fullmatch(r"[1-9][0-9]{0,18}", str(value)) or int(value) >= 2**63:
        raise ValueError("Choose a valid saved revision or project note.")
    return int(value)


def form_subject(fields):
    keys = fields.keys() & {"note_id", "source_task", "source_name", "source_revision"}
    if not keys:
        return {}
    if keys == {"note_id"}:
        return {"note_id": _positive(fields["note_id"])}
    if keys == {"source_task", "source_name", "source_revision"}:
        task_id, name = fields["source_task"], fields["source_name"]
        if (isinstance(task_id, str) and re.fullmatch("[a-f0-9]{32}", task_id)
                and isinstance(name, str) and 1 <= len(name) <= 120):
            return {"source_task": task_id, "source_name": name, "source_revision": _positive(fields["source_revision"])}
    raise ValueError("Choose one existing project note or saved file revision to watch.")


def _resolve(db, subject, starting_revision=None):
    if "note_id" in subject:
        note_id = subject["note_id"]
        row = db.execute("""SELECT n.scope, MAX(r.revision) FROM project_notes n
            JOIN project_note_revisions r ON r.note_id=n.id WHERE n.id=? GROUP BY n.id""", (note_id,)).fetchone()
        if row:
            scope, revision = row
            origin = starting_revision or revision
            return {"kind": "note", "note_id": note_id, "scope": scope, "label": scope,
                    "revision": revision, "url": f"/notes/{note_id}?revision={revision}",
                    "starting_revision": origin, "starting_url": f"/notes/{note_id}?revision={origin}"}
    else:
        task, name, revision = subject["source_task"], subject["source_name"], subject["source_revision"]
        if db.execute("SELECT 1 FROM artifact_revisions WHERE task_id=? AND name=? AND revision=?",
                      (task, name, revision)).fetchone():
            url = f"/tasks/{task}/artifacts/{quote(name, safe='')}?revision={revision}"
            return {"kind": "artifact", "source_task_id": task, "name": name, "revision": revision,
                    "label": f"{name} · revision {revision}", "url": url, "starting_revision": revision,
                    "starting_url": url}
    raise ValueError("The selected note or file revision could not be found.")


def describe_subject(store, subject):
    if subject:
        with store._connection() as db:
            return _resolve(db, form_subject(subject))
    return None


def attach(db, watch_id, subject):
    fields = form_subject(subject)
    resolved = _resolve(db, fields)
    db.execute("INSERT INTO watch_subjects(watch_id,note_id,source_task,source_name,revision) VALUES(?,?,?,?,?)",
               (watch_id, fields.get("note_id"), fields.get("source_task"), fields.get("source_name"),
                resolved["starting_revision"]))


def load(db, watch_id):
    row = db.execute("SELECT note_id,source_task,source_name,revision FROM watch_subjects WHERE watch_id=?",
                     (watch_id,)).fetchone()
    if row:
        note_id, task, name, revision = row
        fields = {"note_id": note_id} if note_id else {"source_task": task, "source_name": name, "source_revision": revision}
        return _resolve(db, fields, revision)
    return None


def pin(store, db, watch_id, task_id):
    row = db.execute("""SELECT source_task,source_name,revision FROM watch_subjects
                        WHERE watch_id=? AND source_task IS NOT NULL""", (watch_id,)).fetchone()
    if row:
        store._pin_saved_work(db, task_id, *row)


def observer(store, subject):
    """Read the current version at check time, leaving the starting revision accessible."""
    from .runner import _read_file

    def observe(start_line, line_count):
        if type(start_line) is not int or start_line < 1 or type(line_count) is not int or not 1 <= line_count <= 100:
            raise ValueError("Use a positive starting line and read 1 to 100 lines or claims.")
        if subject["kind"] == "note":
            note = store.project_note_by_id(subject["note_id"])
            if note is None:
                raise ValueError("The watched project note is unavailable.")
            claims = note["claims"][start_line - 1:start_line - 1 + line_count]
            url = f"/notes/{note['id']}?revision={note['revision']}"
            page = {"lines": [{"number": n, "text": claim["text"], "evidence": claim["sources"],
                               "claim_url": f"{url}#claim-{n}"} for n, claim in enumerate(claims, start_line)],
                    "total_lines": len(note["claims"]), "revision": note["revision"], "source_url": url}
        else:
            name, task = subject["name"], subject["source_task_id"]
            latest = store.artifact_revision(task, name)
            if latest is None:
                raise ValueError("The watched file is unavailable.")
            page = _read_file({name: latest["content"].decode("utf-8")}, name, start_line, line_count)
            page.update(revision=latest["revision"],
                        source_url=f"/tasks/{task}/artifacts/{quote(name, safe='')}?revision={latest['revision']}")
        if not page["lines"] and (page["total_lines"] or start_line != 1):
            raise ValueError("This page starts after the end of the watched item.")
        return {**page, "observed_workspace_subject": True, "starting_url": subject["starting_url"]}
    return observe
