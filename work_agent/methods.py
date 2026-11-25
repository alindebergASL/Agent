"""Scope-based discovery of reusable procedures. Only public index fields are searched."""

import json
import re
import sqlite3


# Generic assignment wording alone is weak evidence for prescribing a procedure.
STOPWORDS = set("""
a an and are as at be been but by can could do does each for from had has have i if in
into is it its me of on or our should than that the their them then these they this to using
was we were what when which will with would you your please save saved method methods
give show make use check attached files results
""".split())


def initialize_method_search(connection: sqlite3.Connection) -> None:
    if connection.execute("SELECT 1 FROM sqlite_schema WHERE name = 'method_search'").fetchone():
        return
    connection.execute("""
        CREATE VIRTUAL TABLE method_search USING fts5(
            title, scope, content='saved_methods', content_rowid='id', tokenize='porter unicode61'
        )
    """)
    connection.execute("INSERT INTO method_search(method_search) VALUES ('rebuild')")
    insert = "INSERT INTO method_search(rowid,title,scope) VALUES (NEW.id,NEW.title,NEW.scope);"
    delete = ("INSERT INTO method_search(method_search,rowid,title,scope) "
              "VALUES ('delete',OLD.id,OLD.title,OLD.scope);")
    for suffix, event, actions in (
        ("add", "INSERT", insert),
        ("edit", "UPDATE OF title, scope", delete + insert),
        ("remove", "DELETE", delete),
    ):
        connection.execute(f"CREATE TRIGGER method_search_{suffix} AFTER {event} ON saved_methods BEGIN {actions} END")


def ranked_methods(connection: sqlite3.Connection, query: str) -> list[dict]:
    terms = dict.fromkeys(re.findall(r"[^\W_]+", query.casefold()))
    expression = " OR ".join(f'"{term}"' for term in terms if term not in STOPWORDS)
    if not expression:
        return []
    return [dict(row) for row in connection.execute("""
        SELECT m.id, m.title, m.scope FROM method_search JOIN saved_methods m ON m.id = method_search.rowid
        WHERE method_search MATCH ? ORDER BY bm25(method_search, 1.0, 2.0), m.id
    """, (expression,))]


def method_page(methods: list[dict], offset: int) -> dict:
    # Bound the context cost of one page in bytes, independent of the number of methods.
    if type(offset) is not int or not 0 <= offset < len(methods):
        raise ValueError("Choose an offset returned by the method index.")
    selected = []
    size = 0
    for index in range(offset, len(methods)):
        item = methods[index]
        size += len(json.dumps(item).encode("utf-8")) + 2
        if selected and size > 8192:
            return {"saved_methods": selected, "next_method_offset": index}
        selected.append(item)
    return {"saved_methods": selected}
