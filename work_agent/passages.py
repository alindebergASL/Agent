"""Search published source passages locally. Indexed rows are derived, never citation targets."""

import csv
import math
import re
from collections import Counter


PREVIEW_CHARS = 600
WORDS = re.compile(r"[^\W_]+", re.UNICODE)
STOP = set("a an and are as at be by for from in into is it of on or that the then this to was with".split())


def chunks(name, content):
    # Match the numbered lines used by read_prior_artifact and the evidence viewer.
    lines = content.decode("utf-8").splitlines()
    lines_start = 0
    if name.lower().endswith(".csv") and lines:
        reader = csv.reader(lines)
        try:
            next(reader)
            end = reader.line_num
            lines_start = end
            header = "\n".join(lines[:end])
            yield 1, end, "", header
            for _ in reader:
                start, end = end + 1, reader.line_num
                text = "\n".join(lines[start - 1:end])
                lines_start = end
                if text.strip():
                    yield start, end, header[:240], text
            return
        except csv.Error:
            # An oversized or irregular CSV is still searchable as physical text.
            pass
        except StopIteration:
            return
    headings = {}
    paragraph = []
    start = 0
    for index, line in enumerate(lines[lines_start:], lines_start + 1):
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line) if name.lower().endswith(".md") else None
        if paragraph and (not line.strip() or heading or len(paragraph) >= 8):
            yield start, index - 1, " / ".join(headings.values())[:240], "\n".join(paragraph)
            paragraph = []
        if heading:
            depth = len(heading[1])
            headings = {level: title for level, title in headings.items() if level < depth}
            headings[depth] = heading[2]
        if line.strip():
            if not paragraph:
                start = index
            paragraph.append(line)
    if paragraph:
        yield start, len(lines), " / ".join(headings.values())[:240], "\n".join(paragraph)


def initialize(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'source_passages'").fetchone():
        return
    db.execute("""CREATE TABLE passage_files (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
        kind TEXT NOT NULL, name TEXT NOT NULL, revision INTEGER, dirty INTEGER NOT NULL DEFAULT 1,
        UNIQUE(task_id, kind, name))""")
    db.execute("CREATE INDEX pending_passages ON passage_files(id) WHERE dirty = 1")
    db.execute("""CREATE VIRTUAL TABLE source_passages USING fts5(
        file_id UNINDEXED, first_line UNINDEXED, last_line UNINDEXED, section, body,
        tokenize='unicode61 remove_diacritics 2')""")
    # A regular index supports replacing one file's passages without scanning the corpus.
    db.execute("CREATE TABLE file_passage_rows (file_id INTEGER NOT NULL, passage_id INTEGER PRIMARY KEY)")
    db.execute("CREATE INDEX passages_for_file ON file_passage_rows(file_id)")
    for table, kind, revision in (("uploads", "upload", "NULL"), ("artifact_revisions", "artifact", "NEW.revision")):
        db.execute(f"""CREATE TRIGGER dirty_passages_{kind} AFTER INSERT ON {table} BEGIN
            INSERT INTO passage_files(task_id, kind, name, revision) VALUES(NEW.task_id, '{kind}', NEW.name, {revision})
            ON CONFLICT(task_id,kind,name) DO UPDATE SET revision=MAX(excluded.revision,revision), dirty=1;
            END""")
        current_revision = "MAX(revision)" if kind == "artifact" else "NULL"
        db.execute(f"""INSERT INTO passage_files(task_id, kind, name, revision)
            SELECT task_id, '{kind}', name, {current_revision} FROM {table} GROUP BY task_id,name""")


def refresh(db, upload_bytes):
    for source in db.execute("SELECT * FROM passage_files WHERE dirty = 1 ORDER BY id").fetchall():
        task, name, file_id = source["task_id"], source["name"], source["id"]
        if source["kind"] == "upload":
            entry = db.execute("SELECT position,content FROM uploads WHERE task_id=? AND name=?", (task, name)).fetchone()
            try:
                content = upload_bytes(task, entry)
            except FileNotFoundError:
                # Legacy volumes may have lost the old positional file. Retry when restored.
                continue
        else:
            content = db.execute("SELECT content FROM artifact_revisions WHERE task_id=? AND name=? AND revision=?",
                                 (task, name, source["revision"])).fetchone()[0]
        db.execute("DELETE FROM source_passages WHERE rowid IN (SELECT passage_id FROM file_passage_rows WHERE file_id=?)", (file_id,))
        db.execute("DELETE FROM file_passage_rows WHERE file_id=?", (file_id,))
        for first, last, section, body in chunks(name, content):
            entry = db.execute("INSERT INTO source_passages(file_id,first_line,last_line,section,body) VALUES(?,?,?,?,?)",
                               (file_id, first, last, section, body))
            db.execute("INSERT INTO file_passage_rows(file_id,passage_id) VALUES(?,?)", (file_id, entry.lastrowid))
        db.execute("UPDATE passage_files SET dirty=0 WHERE id=?", (file_id,))


def match_expression(terms, conjunction=" AND "):
    return conjunction.join(f'"{term}"' for term in dict.fromkeys(terms))


def _matches(db, task, kind, expression, limit):
    # Choose the strongest passage per file before the global result limit.
    # A large CSV can otherwise crowd all other files out with identical hits.
    return db.execute(
        """WITH hits AS MATERIALIZED (
            SELECT p.rowid AS passage_id, f.id AS file_id, p.first_line,
                   bm25(source_passages, 0, 0, 0, 0.25, 1) AS score
            FROM source_passages AS p JOIN passage_files AS f ON f.id=p.file_id
            WHERE source_passages MATCH ? AND f.task_id != ? AND f.dirty=0 AND (?='any' OR f.kind=?)
        ), ranked AS (
            SELECT *, row_number() OVER (PARTITION BY file_id ORDER BY score, first_line) AS position FROM hits
        ), best AS MATERIALIZED (
            SELECT * FROM ranked WHERE position=1 ORDER BY score, file_id LIMIT ?
        )
        SELECT f.task_id AS source_task_id, f.name, f.revision, f.kind,
               t.created_at, substr(t.request, 1, 240) AS request,
               p.first_line, p.last_line, p.section, snippet(source_passages, 4, char(2), char(3), ' … ', 38) AS marked_excerpt
        FROM best JOIN source_passages AS p ON p.rowid=best.passage_id
        JOIN passage_files AS f ON f.id=best.file_id JOIN tasks AS t ON t.id=f.task_id
        WHERE source_passages MATCH ? ORDER BY best.score, best.file_id""",
        (expression, task, kind, kind, limit, expression),
    ).fetchall()


def _excerpt(row):
    # Token-limited FTS snippets can contain very long cells. Keep a matched
    # term visible even if a preceding cell alone exceeds the preview budget.
    marked = row["marked_excerpt"]
    center = max(0, marked.find("\x02"))
    start = max(0, center - PREVIEW_CHARS // 3)
    excerpt = marked[start:].replace("\x02", "").replace("\x03", "")
    return ("… " if start else "") + excerpt[:PREVIEW_CHARS]


def _context(text, excluded):
    return {
        term for term in WORDS.findall(text.casefold())
        if term.isalpha() and len(term) > 2 and term not in STOP and term not in excluded
    }


def search(db, task, kind, terms, metadata, query, paraphrases):
    """Fuse exact metadata, exact passages, model paraphrases and distributional context."""
    scores = Counter()
    files = {}
    passages = {}

    def add(rows, weight, channel):
        seen = set()
        for row in rows:
            key = (row["source_task_id"], row["kind"], row["name"])
            if key in seen:
                continue
            seen.add(key)
            files.setdefault(key, {
                field: row[field] for field in ("source_task_id", "kind", "name", "revision", "request", "created_at")
            })
            scores[key] += weight / (20 + len(seen))
            if channel and key not in passages:
                passages[key] = {
                    "start_line": row["first_line"], "end_line": row["last_line"], "context": row["section"],
                    "text": _excerpt(row), "match": channel,
                }

    add(metadata, 1.0, None)
    direct = _matches(db, task, kind, match_expression(terms), 96)
    add(direct, 1.1, "keyword")
    identifiers = [term for term in terms if any(character.isdigit() for character in term)]
    searched = {match_expression(terms).casefold()}
    for phrase in paraphrases:
        alternate_words = WORDS.findall(phrase)
        if not alternate_words:
            continue
        # A paraphrase can broaden terminology, while the source identifier
        # must still match literally (especially when leading zeros matter).
        expression = match_expression([*identifiers, *alternate_words])
        if expression.casefold() not in searched:
            add(_matches(db, task, kind, expression, 48), 0.65, "paraphrase")
            searched.add(expression.casefold())
    # Local distributional similarity. The query vector is the mean context of
    # literal seed passages. Candidates can share context without a query term.
    query_words = {t.casefold() for t in terms}
    if direct and all(t.isalpha() for t in terms) and not re.search(r"\.[\w]+$", query):
        vector = Counter()
        for row in direct[:12]:
            vector.update(_context(_excerpt(row), query_words))
        anchors = sorted(vector, key=lambda term: (-vector[term], term))[:16]
        if len(anchors) >= 2:
            candidates = _matches(db, task, kind, match_expression(anchors, " OR "), 96)
            magnitude = math.sqrt(sum(value * value for value in vector.values()))
            related = []
            for row in candidates:
                words = _context(_excerpt(row), query_words)
                overlap = vector.keys() & words
                cosine = sum(vector[t] for t in overlap) / (magnitude * math.sqrt(len(words))) if words else 0
                if len(overlap) >= 2 and cosine >= 0.35:
                    related.append((cosine, row))
            related.sort(key=lambda pair: -pair[0])
            add([row for _, row in related], 0.35, "related context")
    ranked = sorted(files, key=lambda k: (k[2].casefold() != query.strip().casefold(), -scores[k]))
    for key in ranked[:8]:
        yield {**files[key], **({"passage": passages[key]} if key in passages else {})}
