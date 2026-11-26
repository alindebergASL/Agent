"""Search prior, published findings within one watch without loading its whole history."""

import json
import re


def initialize(db):
    if db.execute("SELECT 1 FROM sqlite_schema WHERE name='watch_finding_search'").fetchone():
        return
    db.execute("""CREATE VIRTUAL TABLE watch_finding_search USING fts5(
               event_key,title,why,content='watch_findings',content_rowid='id',
               tokenize='porter unicode61')""")
    db.execute("INSERT INTO watch_finding_search(watch_finding_search) VALUES('rebuild')")
    # Published findings never change. Acknowledgement affects only the dashboard.
    db.execute("""CREATE TRIGGER index_watch_finding AFTER INSERT ON watch_findings BEGIN
        INSERT INTO watch_finding_search(rowid,event_key,title,why)
        VALUES(new.id,new.event_key,new.title,new.why); END""")


def search(store, task_id, query, offset):
    if not isinstance(query, str) or len(query) > 240 or type(offset) is not int or not 0 <= offset < 2**63 - 10:
        raise ValueError("Use up to 240 characters for the search and a nonnegative offset.")
    terms = list(dict.fromkeys(re.findall(r"[^\W_]+", query)))
    if not terms:
        raise ValueError("Search for an identifier or a few words from a previous finding.")
    expression = " AND ".join(f'"{term}"' for term in terms)
    with store._connection() as db:
        rows = db.execute("""SELECT f.task_id, c.due_at, f.event_key AS key, f.title, f.why, f.evidence
            FROM watch_checks active JOIN watch_findings f ON f.watch_id=active.watch_id
            JOIN watch_checks c ON c.task_id=f.task_id AND c.id<active.id
            JOIN watch_finding_search ON watch_finding_search.rowid=f.id
            WHERE active.task_id=? AND watch_finding_search MATCH ?
            ORDER BY bm25(watch_finding_search,4,2,1), f.id DESC LIMIT 9 OFFSET ?""",
            (task_id, expression, offset)).fetchall()
    results = []
    for row in rows[:8]:
        evidence = json.loads(row["evidence"])
        results.append({**dict(row), "evidence": evidence[:3], "evidence_count": len(evidence),
                        "assignment_url": f"/tasks/{row['task_id']}"})
    return {"findings": results, "next_offset": offset + 8 if len(rows) > 8 else None}
