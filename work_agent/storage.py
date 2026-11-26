import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .methods import initialize_method_search, ranked_methods
from . import memory_context, project_notes
from . import evidence_changes, evidence_links
from . import passages, pdf_documents, watch_history, watch_subjects, watches
from .scheduling import latest_occurrence, next_occurrence, parse_schedule_time, scheduled_request


ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.(csv|md)")
MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_FILES = 8
MAX_SCHEDULE_REQUEST_CHARS = 8_000
METHOD_FIELD_LIMITS = {"title": 120, "scope": 500, "procedure": 4000, "evidence": 1000}
INTERRUPTED = "The worker restarted before this assignment finished."
SCHEMA_VERSION = 2
STATUSES = "'queued','running','completed','failed','interrupted','stopped','superseded','waiting'"

TASKS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {{table}} (
    id TEXT PRIMARY KEY,
    request TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ({STATUSES})),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    response TEXT,
    error TEXT
);
"""

TURNS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {{table}} (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    request TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ({STATUSES})),
    kind TEXT NOT NULL CHECK (kind IN ('initial','follow-up','correction','continuation','answer')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    response TEXT,
    error TEXT
);
"""

SCHEDULE_OCCURRENCES_TABLE = """
CREATE TABLE IF NOT EXISTS schedule_occurrences (
    schedule_id INTEGER NOT NULL REFERENCES weekly_schedules(id),
    due_at INTEGER NOT NULL,
    task_id TEXT NOT NULL PRIMARY KEY REFERENCES tasks(id),
    manual INTEGER NOT NULL DEFAULT 0 CHECK (manual IN (0, 1)),
    prior_resolved INTEGER NOT NULL DEFAULT 0 CHECK (prior_resolved IN (0, 1))
)
"""

# Exactly one foreign key applies: a published revision, or the immutable upload
# in the original task. Both share the existing citation ID namespace.
PRIOR_ARTIFACTS_TABLE = """
CREATE TABLE IF NOT EXISTS prior_artifacts (
    reference_id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    source_task_id TEXT NOT NULL,
    name TEXT NOT NULL,
    revision INTEGER,
    upload_task_id TEXT GENERATED ALWAYS AS (CASE WHEN revision IS NULL THEN source_task_id END) VIRTUAL,
    FOREIGN KEY (source_task_id, name, revision) REFERENCES artifact_revisions(task_id, name, revision),
    FOREIGN KEY (upload_task_id, name) REFERENCES uploads(task_id, name),
    UNIQUE (task_id, source_task_id, name, revision),
    UNIQUE (task_id, upload_task_id, name)
)
"""

SCHEMA = TASKS_TABLE.format(table="tasks") + TURNS_TABLE.format(table="turns") + """
CREATE TABLE IF NOT EXISTS uploads (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    content BLOB,
    turn_id INTEGER REFERENCES turns(id),
    PRIMARY KEY (task_id, position),
    UNIQUE (task_id, name)
);
CREATE TABLE IF NOT EXISTS pdf_upload_pages (
    task_id TEXT NOT NULL, name TEXT NOT NULL,
    number INTEGER NOT NULL CHECK(number > 0),
    start_line INTEGER NOT NULL CHECK(start_line > 0), end_line INTEGER NOT NULL CHECK(end_line >= start_line),
    text TEXT NOT NULL,
    PRIMARY KEY(task_id, name, number),
    FOREIGN KEY(task_id, name) REFERENCES uploads(task_id, name)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS turns_task ON turns(task_id, id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    message TEXT NOT NULL,
    turn_id INTEGER REFERENCES turns(id)
);
CREATE TABLE IF NOT EXISTS run_steps (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    payload TEXT NOT NULL,
    turn_id INTEGER REFERENCES turns(id)
);
CREATE INDEX IF NOT EXISTS run_steps_task ON run_steps(task_id, id);
CREATE TABLE IF NOT EXISTS artifact_revisions (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    name TEXT NOT NULL,
    revision INTEGER NOT NULL,
    author TEXT NOT NULL CHECK (author IN ('agent', 'user')),
    content BLOB NOT NULL,
    turn_id INTEGER REFERENCES turns(id),
    PRIMARY KEY (task_id, name, revision)
);
CREATE TABLE IF NOT EXISTS turn_artifacts (
    turn_id INTEGER NOT NULL REFERENCES turns(id),
    name TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY (turn_id, name)
);
CREATE TABLE IF NOT EXISTS staged_artifacts (
    turn_id INTEGER NOT NULL REFERENCES turns(id),
    name TEXT NOT NULL,
    content BLOB NOT NULL,
    PRIMARY KEY (turn_id, name)
);
CREATE TABLE IF NOT EXISTS staged_methods (
    turn_id INTEGER PRIMARY KEY REFERENCES turns(id),
    title TEXT NOT NULL,
    scope TEXT NOT NULL,
    procedure TEXT NOT NULL,
    evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saved_methods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_task_id TEXT NOT NULL REFERENCES tasks(id),
    title TEXT NOT NULL,
    scope TEXT NOT NULL,
    procedure TEXT NOT NULL,
    evidence TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS saved_methods_recent ON saved_methods(updated_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS weekly_schedules (
    id INTEGER PRIMARY KEY,
    request TEXT NOT NULL,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    local_time TEXT NOT NULL,
    next_run_at INTEGER NOT NULL,
    stopped_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_schedule ON weekly_schedules(1) WHERE stopped_at IS NULL;
""" + SCHEDULE_OCCURRENCES_TABLE + ";" + PRIOR_ARTIFACTS_TABLE + ";"


class RevisionConflict(RuntimeError):
    """A saved artifact changed after the caller read it."""


class InactiveTurn(RuntimeError):
    """The worker's turn is no longer the active running turn."""


class Store(project_notes.ProjectNoteStore, evidence_changes.EvidenceChangeStore, watches.WatchStore):
    def __init__(self, root: Path):
        self.root = Path(root)

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.root / "agent.sqlite3")) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection

    def _workspace(self, task_id: str) -> Path:
        return self.root / "runs" / task_id

    def initialize(self) -> None:
        (self.root / "runs").mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            # Rebuilding parent tables must leave the existing child table references intact.
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                self._upgrade_turn_schema(connection)
            for statement in (SCHEMA + project_notes.SCHEMA + evidence_changes.SCHEMA + watches.SCHEMA).split(";"):
                if statement.strip():
                    connection.execute(statement)
            if version < SCHEMA_VERSION:
                columns = {column["name"] for column in connection.execute("PRAGMA table_info(uploads)")}
                if "content" not in columns:
                    connection.execute("ALTER TABLE uploads ADD COLUMN content BLOB")
                if "turn_id" not in columns:
                    connection.execute("ALTER TABLE uploads ADD COLUMN turn_id INTEGER REFERENCES turns(id)")
            columns = connection.execute("PRAGMA table_info(saved_methods)")
            if "revision" not in {column["name"] for column in columns}:
                connection.execute("ALTER TABLE saved_methods ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
            for table in ("events", "run_steps"):
                columns = connection.execute(f"PRAGMA table_info({table})")
                if "turn_id" not in {column["name"] for column in columns}:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN turn_id INTEGER REFERENCES turns(id)")
            connection.execute("CREATE INDEX IF NOT EXISTS events_turn ON events(turn_id, id)")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(schedule_occurrences)")}
            if "manual" not in columns:
                connection.execute("ALTER TABLE schedule_occurrences RENAME TO schedule_occurrences_legacy")
                connection.execute(SCHEDULE_OCCURRENCES_TABLE)
                connection.execute(
                    """INSERT INTO schedule_occurrences (schedule_id, due_at, task_id)
                    SELECT schedule_id, due_at, task_id FROM schedule_occurrences_legacy"""
                )
                connection.execute("DROP TABLE schedule_occurrences_legacy")
            elif "prior_resolved" not in columns:
                connection.execute(
                    "ALTER TABLE schedule_occurrences ADD COLUMN prior_resolved INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (prior_resolved IN (0, 1))"
                )
            if "prior_resolved" not in columns:
                # Preserve what already-started assignments actually saw on older deployments.
                connection.execute(
                    """UPDATE schedule_occurrences SET prior_resolved = 1
                    WHERE task_id NOT IN (SELECT task_id FROM turns WHERE kind = 'initial' AND status = 'queued')"""
                )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS one_scheduled_occurrence
                ON schedule_occurrences (schedule_id, due_at) WHERE manual = 0"""
            )
            self._migrate_tasks(connection)
            prior_columns = connection.execute("PRAGMA table_info(prior_artifacts)")
            if any(col["name"] == "revision" and col["notnull"] for col in prior_columns):
                connection.execute("ALTER TABLE prior_artifacts RENAME TO prior_artifacts_legacy")
                connection.execute(PRIOR_ARTIFACTS_TABLE)
                connection.execute(
                    """INSERT INTO prior_artifacts (reference_id, task_id, source_task_id, name, revision)
                       SELECT reference_id, task_id, source_task_id, name, revision FROM prior_artifacts_legacy"""
                )
                connection.execute("DROP TABLE prior_artifacts_legacy")
            watch_history.initialize(connection)
            watch_subjects.initialize(connection)
            initialize_method_search(connection)
            memory_context.initialize(connection)
            evidence_links.initialize(connection, self._upload_bytes)
            self._initialize_saved_work_search(connection)
            passages.initialize(connection)
            passages.refresh(connection, self._upload_bytes)
            connection.execute(
                "UPDATE turns SET status = 'interrupted', error = ? WHERE status = 'running'",
                (INTERRUPTED,),
            )
            connection.execute(
                "UPDATE tasks SET status = 'interrupted', error = ? WHERE status = 'running'",
                (INTERRUPTED,),
            )
            connection.execute("""DELETE FROM evidence_changes WHERE published=0 AND turn_id IN
                               (SELECT id FROM turns WHERE status='interrupted')""")
            for table in ("staged_artifacts", "staged_methods", "staged_project_notes", "staged_watch_assessments"):
                connection.execute(
                    f"DELETE FROM {table} WHERE turn_id IN (SELECT id FROM turns WHERE status = 'interrupted')"
                )
            if version < SCHEMA_VERSION:
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise RuntimeError("Assignment database migration found a broken reference.")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _initialize_saved_work_search(self, connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(saved_work_search)")}
        if "kind" in columns:
            return
        connection.execute("DROP TRIGGER IF EXISTS index_published_artifact")
        connection.execute("DROP TRIGGER IF EXISTS index_original_upload")
        connection.execute("DROP TABLE IF EXISTS saved_work_search")
        connection.execute(
            """CREATE VIRTUAL TABLE saved_work_search USING fts5(
                source_task_id UNINDEXED, name, request, kind UNINDEXED, tokenize='unicode61 remove_diacritics 2')"""
        )
        # Backfill once, then index new files in the same transaction as submission
        # or publication. Contents and later edits never enter this metadata index.
        for table, kind, trigger, predicate in (
            ("artifact_revisions", "artifact", "index_published_artifact", "saved.revision = 1"),
            ("uploads", "upload", "index_original_upload", "1"),
        ):
            insert = f"""INSERT INTO saved_work_search (source_task_id, name, request, kind)
                SELECT saved.task_id, saved.name,
                    CASE WHEN turn.request IS NOT NULL AND turn.request != task.request
                         THEN task.request || char(10) || turn.request ELSE task.request END, '{kind}'
                FROM {table} AS saved JOIN tasks AS task ON task.id = saved.task_id
                LEFT JOIN turns AS turn ON turn.id = saved.turn_id WHERE {predicate}"""
            connection.execute(insert)
            when = "WHEN NEW.revision = 1" if kind == "artifact" else ""
            connection.execute(
                f"CREATE TRIGGER {trigger} AFTER INSERT ON {table} {when} BEGIN "
                + insert + " AND saved.task_id = NEW.task_id AND saved.name = NEW.name; END"
            )

    def _upgrade_turn_schema(self, connection: sqlite3.Connection) -> None:
        existing = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name IN ('tasks', 'turns')"
        )}
        if "tasks" in existing:
            connection.execute(TASKS_TABLE.format(table="tasks_upgrade"))
            connection.execute(
                """
                INSERT INTO tasks_upgrade (rowid, id, request, status, created_at, response, error)
                SELECT rowid, id, request, status, created_at, response, error FROM tasks
                """
            )
            connection.execute("DROP TABLE tasks")
            connection.execute("ALTER TABLE tasks_upgrade RENAME TO tasks")
        if "turns" in existing:
            columns = connection.execute("PRAGMA table_info(turns)")
            kind = "kind" if "kind" in {column["name"] for column in columns} else """
                CASE WHEN id = (
                    SELECT MIN(first.id) FROM turns AS first WHERE first.task_id = turns.task_id
                ) THEN 'initial' ELSE 'follow-up' END
            """
            connection.execute(TURNS_TABLE.format(table="turns_upgrade"))
            connection.execute(
                f"""
                INSERT INTO turns_upgrade (id, task_id, request, status, created_at, response, error, kind)
                SELECT id, task_id, request, status, created_at, response, error, {kind}
                FROM turns
                """
            )
            connection.execute("DROP TABLE turns")
            connection.execute("ALTER TABLE turns_upgrade RENAME TO turns")

    def _migrate_tasks(self, connection: sqlite3.Connection) -> None:
        legacy = connection.execute(
            """
            SELECT * FROM tasks WHERE NOT EXISTS (SELECT 1 FROM turns WHERE task_id = tasks.id)
            ORDER BY tasks.rowid
            """
        )
        for task in legacy:
            task_id = task["id"]
            turn_id = connection.execute(
                """
                INSERT INTO turns (task_id, request, status, created_at, response, error, kind)
                VALUES (?, ?, ?, ?, ?, ?, 'initial') RETURNING id
                """,
                (task_id, task["request"], task["status"], task["created_at"], task["response"], task["error"]),
            ).fetchone()["id"]
            for table in ("events", "run_steps"):
                connection.execute(f"UPDATE {table} SET turn_id = ? WHERE task_id = ?", (turn_id, task_id))
            outputs = self._workspace(task_id) / "outputs"
            if task["status"] == "completed" and outputs.is_dir():
                for path in outputs.iterdir():
                    if ARTIFACT_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink():
                        connection.execute(
                            """
                            INSERT INTO artifact_revisions (task_id, name, revision, author, content, turn_id)
                            VALUES (?, ?, 1, 'agent', ?, ?)
                            """,
                            (task_id, path.name, path.read_bytes(), turn_id),
                        )

    def submit(
        self, request: str, uploads: dict[str, bytes], *, prior_revision: tuple[str, str, int | None] | None = None
    ) -> str:
        task_id = uuid.uuid4().hex
        sources = self._prepare_uploads(uploads)
        with self._connection() as connection:
            turn_id = self._insert_task(connection, task_id, request)
            if prior_revision is not None:
                self._pin_saved_work(connection, task_id, *prior_revision)
            self._add_uploads(connection, task_id, turn_id, sources)
        return task_id

    def _insert_task(self, connection: sqlite3.Connection, task_id: str, request: str) -> int:
        connection.execute(
            "INSERT INTO tasks (id, request, status) VALUES (?, ?, 'queued')",
            (task_id, request),
        )
        return connection.execute(
            "INSERT INTO turns (task_id, request, status, kind) VALUES (?, ?, 'queued', 'initial') RETURNING id",
            (task_id, request),
        ).fetchone()["id"]

    def create_schedule(self, request: str, weekday: int, local_time: str, *, now: datetime) -> int:
        request = request.strip() if isinstance(request, str) else ""
        if not 0 < len(request) <= MAX_SCHEDULE_REQUEST_CHARS or "\x00" in request:
            raise ValueError(f"Enter a weekly request of up to {MAX_SCHEDULE_REQUEST_CHARS:,} characters.")
        if type(weekday) is not int or not 0 <= weekday <= 6:
            raise ValueError("Choose a day of the week.")
        at = parse_schedule_time(local_time)
        self._schedule_epoch(now)
        next_run_at = next_occurrence(weekday, at, now)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute("SELECT 1 FROM weekly_schedules WHERE stopped_at IS NULL").fetchone()
            if active is not None:
                raise ValueError("Stop the current weekly responsibility before starting another.")
            row = connection.execute(
                """
                INSERT INTO weekly_schedules (request, weekday, local_time, next_run_at)
                VALUES (?, ?, ?, ?) RETURNING id
                """,
                (request, weekday, at.isoformat(timespec="minutes"), next_run_at),
            ).fetchone()
            return row["id"]

    def get_schedule(self) -> dict | None:
        with self._connection() as connection:
            connection.execute("BEGIN")
            schedule = connection.execute(
                "SELECT * FROM weekly_schedules ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if schedule is None:
                return None
            latest_run = connection.execute(
                """
                SELECT occurrence.task_id, occurrence.due_at, occurrence.manual, task.status
                FROM schedule_occurrences AS occurrence
                JOIN tasks AS task ON task.id = occurrence.task_id
                WHERE occurrence.schedule_id = ?
                ORDER BY task.rowid DESC LIMIT 1
                """,
                (schedule["id"],),
            ).fetchone()
            brief_files = self._latest_brief_files(connection, schedule["id"])
            latest_brief = None
            if brief_files:
                latest_brief = {
                    "task_id": brief_files[0]["task_id"],
                    "due_at": brief_files[0]["due_at"],
                    "manual": brief_files[0]["manual"],
                    "names": [row["name"] for row in brief_files],
                }
            return {
                **dict(schedule),
                "latest_run": dict(latest_run) if latest_run else None,
                "latest_brief": latest_brief,
            }

    def _latest_brief_files(self, connection, schedule_id: int, *, before_task_id: str | None = None):
        # The first completed turn that published Markdown defines this occurrence's brief files.
        # Later user edits and follow-ups can revise those files without adding unrelated ones.
        return connection.execute(
            """
            WITH brief_turn AS (
                SELECT occurrence.task_id, occurrence.due_at, occurrence.manual, turn.id AS turn_id
                FROM schedule_occurrences AS occurrence
                JOIN tasks AS task ON task.id = occurrence.task_id
                JOIN turns AS turn ON turn.task_id = task.id AND turn.status = 'completed'
                JOIN artifact_revisions AS artifact ON artifact.turn_id = turn.id
                WHERE occurrence.schedule_id = ? AND artifact.name LIKE '%.md'
                    AND (? IS NULL OR task.rowid < (SELECT rowid FROM tasks WHERE id = ?))
                ORDER BY task.rowid DESC, turn.id ASC LIMIT 1
            )
            SELECT brief_turn.task_id, brief_turn.due_at, brief_turn.manual, artifact.name,
                (SELECT MAX(revision) FROM artifact_revisions
                 WHERE task_id = brief_turn.task_id AND name = artifact.name) AS revision
            FROM brief_turn JOIN artifact_revisions AS artifact USING (task_id, turn_id)
            WHERE artifact.name LIKE '%.md' ORDER BY artifact.name
            """,
            (schedule_id, before_task_id, before_task_id),
        ).fetchall()

    def _pin_previous_brief(self, connection, task_id: str) -> None:
        # Freeze the selection (including no previous brief) as part of the first claim.
        occurrence = connection.execute(
            """UPDATE schedule_occurrences SET prior_resolved = 1
            WHERE task_id = ? AND prior_resolved = 0 RETURNING schedule_id""",
            (task_id,),
        ).fetchone()
        if occurrence:
            files = self._latest_brief_files(connection, occurrence["schedule_id"], before_task_id=task_id)
            connection.executemany(
                "INSERT INTO prior_artifacts (task_id, source_task_id, name, revision) VALUES (?, ?, ?, ?)",
                ((task_id, row["task_id"], row["name"], row["revision"]) for row in files),
            )

    def _prior_artifacts(self, connection, task_id: str, reference_id: int | None = None) -> list[dict]:
        query = """SELECT pin.reference_id, pin.source_task_id, pin.name, pin.revision,
                   CASE WHEN pin.revision IS NULL THEN 'upload' ELSE 'artifact' END AS kind,
                   COALESCE(saved.author, 'user') AS author
            FROM prior_artifacts AS pin LEFT JOIN artifact_revisions AS saved
                ON saved.task_id = pin.source_task_id AND saved.name = pin.name AND saved.revision = pin.revision
            WHERE pin.task_id = ?"""
        if reference_id is None:
            rows = connection.execute(query + " ORDER BY pin.reference_id", (task_id,))
        else:
            rows = connection.execute(query + " AND pin.reference_id = ?", (task_id, reference_id))
        references = [dict(row) for row in rows]
        for source in references:
            if source["kind"] == "upload":
                layout = self._pdf_layout(connection, source["source_task_id"], source["name"])
                if layout:
                    source["pdf_pages"] = layout
        return references

    def prior_artifact(self, task_id: str, reference_id: int) -> dict | None:
        if type(reference_id) is not int or not 0 < reference_id <= 2**63 - 1:
            return None
        with self._connection() as connection:
            rows = self._prior_artifacts(connection, task_id, reference_id)
            return rows[0] if rows else None

    def search_saved_work(self, task_id: str, query: str, kind: str = "any", paraphrases=()) -> list[dict]:
        if kind not in ("any", "upload", "artifact"):
            raise ValueError("Choose upload, artifact, or any as the file kind.")
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("Search with up to 200 characters of keywords or a filename.")
        if not isinstance(paraphrases, (list, tuple)) or len(paraphrases) > 3 or any(
            not isinstance(phrase, str) or not 1 <= len(phrase.strip()) <= 200 for phrase in paraphrases
        ):
            raise ValueError("Supply up to three short alternative search phrases.")
        terms = re.findall(r"[^\W_]+", unicodedata.normalize("NFC", query))
        if not terms:
            # Valid upload names may consist entirely of symbols, which FTS does not tokenize.
            with self._connection() as connection:
                rows = connection.execute(
                    """SELECT hit.source_task_id, hit.name, hit.kind, NULL AS revision,
                           substr(hit.request, 1, 240) AS request, task.created_at
                       FROM saved_work_search AS hit JOIN tasks AS task ON task.id = hit.source_task_id
                       WHERE hit.name = ? AND hit.source_task_id != ? AND hit.kind = 'upload'
                           AND ? IN ('any', 'upload')
                       ORDER BY task.created_at DESC, task.rowid DESC LIMIT 8""",
                    (query, task_id, kind),
                ).fetchall()
            if rows:
                return [dict(row) for row in rows]
            raise ValueError("Search with a keyword or exact filename.")
        # Treat punctuation and FTS operators as user input. Every term must match.
        match = passages.match_expression(terms)
        with self._connection() as connection:
            # Pending writes are indexed once, under a writer lock. Each search
            # sees metadata and passages from one consistent source snapshot.
            connection.execute("BEGIN")
            if connection.execute("SELECT 1 FROM passage_files WHERE dirty=1 LIMIT 1").fetchone():
                connection.rollback()
                connection.execute("BEGIN IMMEDIATE")
                passages.refresh(connection, self._upload_bytes)
            rows = connection.execute(
                """SELECT hit.source_task_id, hit.name, hit.kind,
                       CASE WHEN hit.kind = 'artifact' THEN (SELECT MAX(revision) FROM artifact_revisions
                        WHERE task_id = hit.source_task_id AND name = hit.name) END AS revision,
                       substr(snippet(saved_work_search, 2, '', '', ' … ', 28), 1, 240) AS request,
                       task.created_at
                   FROM saved_work_search AS hit JOIN tasks AS task ON task.id = hit.source_task_id
                   WHERE saved_work_search MATCH ? AND hit.source_task_id != ?
                         AND (? = 'any' OR hit.kind = ?)
                   ORDER BY (hit.name = ? COLLATE NOCASE) DESC, bm25(saved_work_search, 0, 4, 1),
                            task.created_at DESC, task.rowid DESC, hit.name, hit.kind LIMIT 8""",
                (match, task_id, kind, kind, query.strip()),
            )
            return list(passages.search(connection, task_id, kind, terms, rows, query, paraphrases))

    def search_pdf_pages(self, task_id, name, query, offset=0):
        if not isinstance(query, str) or len(query) > 200 or not (terms := passages.WORDS.findall(query)):
            raise ValueError("Search with up to 200 characters of keywords.")
        if type(offset) is not int or not 0 <= offset < 2**63:
            raise ValueError("Supply a nonnegative search offset.")
        with self._connection() as db:
            db.execute("BEGIN")
            source = db.execute("""SELECT id,dirty FROM passage_files f
                WHERE (task_id,name,kind)=(?,?,'upload') AND EXISTS (
                    SELECT 1 FROM pdf_upload_pages p WHERE p.task_id=f.task_id AND p.name=f.name)
                """, (task_id, name)).fetchone()
            if source is None:
                raise ValueError("Choose a current or pinned PDF upload.")
            if source["dirty"]:
                db.rollback()
                db.execute("BEGIN IMMEDIATE")
                passages.refresh(db, self._upload_bytes, file_id=source["id"])
            return passages.search_pdf(db, source["id"], terms, offset)

    def _pin_saved_work(self, connection, task_id: str, source_task_id: str, name: str, revision: int | None) -> dict:
        if not isinstance(source_task_id, str) or task_id == source_task_id or not isinstance(name, str):
            raise ValueError("Choose a file from another assignment.")
        if revision is None:
            source = "SELECT task_id, name, NULL FROM uploads WHERE task_id = ? AND name = ?"
            source_args = (source_task_id, name)
        elif type(revision) is int and 0 < revision <= 2**63 - 1 and ARTIFACT_NAME.fullmatch(name):
            source = "SELECT task_id, name, revision FROM artifact_revisions WHERE task_id = ? AND name = ? AND revision = ?"
            source_args = (source_task_id, name, revision)
        else:
            raise ValueError("Choose a published revision from another assignment.")
        connection.execute(
            f"""INSERT INTO prior_artifacts (task_id, source_task_id, name, revision)
                SELECT consumer.id, source.* FROM ({source}) AS source
                JOIN tasks AS consumer ON consumer.id = ? WHERE TRUE ON CONFLICT DO NOTHING""",
            (*source_args, task_id),
        )
        pin = connection.execute(
            """SELECT reference_id FROM prior_artifacts
               WHERE task_id = ? AND source_task_id = ? AND name = ? AND revision IS ?""",
            (task_id, source_task_id, name, revision),
        ).fetchone()
        if pin is None:
            raise ValueError("Choose a published revision from another assignment.")
        return self._prior_artifacts(connection, task_id, pin["reference_id"])[0]

    def pin_saved_work(self, task_id: str, source_task_id: str, name: str, revision: int | None) -> dict:
        with self._connection() as connection:
            return self._pin_saved_work(connection, task_id, source_task_id, name, revision)

    def read_prior_artifact(self, task_id: str, reference_id: int) -> bytes | None:
        return self._read_prior(task_id, reference_id, self.read_upload)

    def read_prior_text(self, task_id: str, reference_id: int) -> bytes | None:
        return self._read_prior(task_id, reference_id, self.read_upload_text)

    def _read_prior(self, task_id, reference_id, read_upload):
        pin = self.prior_artifact(task_id, reference_id)
        if pin is None:
            return None
        if pin["kind"] == "upload":
            return read_upload(pin["source_task_id"], pin["name"])
        saved = self.artifact_revision(pin["source_task_id"], pin["name"], pin["revision"])
        return saved["content"] if saved else None

    def stop_schedule(self, schedule_id: int, *, now: datetime) -> None:
        stopped_at = self._schedule_epoch(now)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT stopped_at FROM weekly_schedules WHERE id = ?", (schedule_id,)).fetchone()
            if row is None:
                raise KeyError(schedule_id)
            if row["stopped_at"] is not None:
                raise ValueError("This weekly responsibility was already stopped.")
            connection.execute("UPDATE weekly_schedules SET stopped_at = ? WHERE id = ?", (stopped_at, schedule_id))

    def enqueue_due_schedule(self, *, now: datetime) -> str | None:
        now_epoch = self._schedule_epoch(now)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            schedule = connection.execute(
                "SELECT * FROM weekly_schedules WHERE stopped_at IS NULL AND next_run_at <= ?",
                (now_epoch,),
            ).fetchone()
            if schedule is None:
                return None
            at = parse_schedule_time(schedule["local_time"])
            due_at = latest_occurrence(schedule["weekday"], at, now)
            next_run_at = next_occurrence(schedule["weekday"], at, now)
            task_id = self._enqueue_occurrence(connection, schedule, due_at, now, manual=False)
            connection.execute(
                "UPDATE weekly_schedules SET next_run_at = ? WHERE id = ?",
                (next_run_at, schedule["id"]),
            )
            return task_id

    def enqueue_schedule_now(self, schedule_id: int, *, now: datetime) -> str:
        due_at = self._schedule_epoch(now)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            schedule = connection.execute("SELECT * FROM weekly_schedules WHERE id = ?", (schedule_id,)).fetchone()
            if schedule is None:
                raise KeyError(schedule_id)
            if schedule["stopped_at"] is not None:
                raise ValueError("This weekly responsibility was already stopped.")
            active = connection.execute(
                """
                SELECT task.id FROM schedule_occurrences AS occurrence
                JOIN tasks AS task ON task.id = occurrence.task_id
                WHERE occurrence.schedule_id = ? AND occurrence.manual = 1
                    AND task.status IN ('queued', 'running', 'waiting')
                ORDER BY task.rowid DESC LIMIT 1
                """,
                (schedule_id,),
            ).fetchone()
            if active is not None:
                return active["id"]
            return self._enqueue_occurrence(connection, schedule, due_at, now, manual=True)

    def _enqueue_occurrence(self, connection, schedule, due_at: int, now: datetime, *, manual: bool) -> str:
        task_id = uuid.uuid4().hex
        self._insert_task(connection, task_id, scheduled_request(schedule["request"], due_at, now, manual=manual))
        connection.execute(
            """INSERT INTO schedule_occurrences (schedule_id, due_at, task_id, manual)
            VALUES (?, ?, ?, ?)""",
            (schedule["id"], due_at, task_id, manual),
        )
        return task_id

    def _schedule_epoch(self, now: datetime) -> int:
        if now.utcoffset() is None:
            raise ValueError("Schedule time must include a time zone.")
        return int(now.timestamp())

    def list_tasks(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM tasks ORDER BY rowid DESC")
            return [dict(row) for row in rows]

    def get_task(self, task_id: str) -> dict | None:
        with self._connection() as connection:
            connection.execute("BEGIN")
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            task = dict(row)
            watch = connection.execute("SELECT watch_id FROM watch_checks WHERE task_id=?", (task_id,)).fetchone()
            task["watch_id"] = watch["watch_id"] if watch else None
            task["uploads"] = [upload["name"] for upload in self._uploads(connection, task_id)]
            task["turns"] = self._turns(connection, task_id)
            task["events"] = [
                event["message"]
                for event in connection.execute(
                    "SELECT message FROM events WHERE turn_id = ? ORDER BY id", (task["turns"][-1]["id"],)
                )
            ]
            task["artifacts"] = [
                row["name"]
                for row in connection.execute(
                    "SELECT DISTINCT name FROM artifact_revisions WHERE task_id = ? ORDER BY name", (task_id,)
                )
            ]
            task["prior_artifacts"] = self._prior_artifacts(connection, task_id)
            task["can_follow_up"] = self._can_follow_up(connection, task_id, task["status"])
            return task

    def _turns(self, connection: sqlite3.Connection, task_id: str) -> list[dict]:
        turns = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, request, status, kind, created_at, response, error
                FROM turns WHERE task_id = ? ORDER BY id
                """,
                (task_id,),
            )
        ]
        if turns:
            by_id = {turn["id"]: turn for turn in turns}
            for source in self._uploads(connection, task_id):
                # All uploads on pre-clarification volumes arrived with the initial turn.
                owner = turns[0] if source["turn_id"] is None else by_id[source["turn_id"]]
                owner.setdefault("attached_files", []).append(source["name"])
        return turns

    def _can_follow_up(self, connection: sqlite3.Connection, task_id: str, status: str) -> bool:
        if status in ("completed", "stopped"):
            return True
        return status == "failed" and connection.execute(
            "SELECT 1 FROM turns WHERE task_id = ? AND status = 'completed' LIMIT 1", (task_id,)
        ).fetchone() is not None

    def follow_up(self, task_id: str, request: str, uploads: dict[str, bytes] | None = None) -> int:
        request = request.strip()
        if not request:
            raise ValueError("Enter a follow-up request.")
        sources = self._prepare_uploads(uploads)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if not self._can_follow_up(connection, task_id, row["status"]):
                raise ValueError("This assignment is not ready for a follow-up.")
            return self._queue_turn(connection, task_id, request, "follow-up", sources)

    def wait_for_input(self, task_id: str, question: str, *, turn_id: int) -> None:
        question = question.strip()
        if not question:
            raise ValueError("Enter a question for the user.")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = self._running_turn(connection, task_id, turn_id)
            self._discard_drafts(connection, active)
            connection.execute("UPDATE turns SET status = 'waiting', response = ? WHERE id = ?", (question, active))
            connection.execute("UPDATE tasks SET status = 'waiting', response = ? WHERE id = ?", (question, task_id))

    def answer(
        self, task_id: str, expected_turn_id: int, request: str, uploads: dict[str, bytes] | None = None,
    ) -> int:
        request = request.strip()
        if not request:
            raise ValueError("Enter an answer.")
        sources = self._prepare_uploads(uploads)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            turn = self._expected_turn(connection, task_id, expected_turn_id)
            if turn["status"] != "waiting":
                raise ValueError("This assignment is no longer waiting for an answer.")
            return self._queue_turn(connection, task_id, request, "answer", sources)

    def _queue_turn(
        self, connection: sqlite3.Connection, task_id: str, request: str,
        kind: str, sources=(),
    ) -> int:
        turn_id = connection.execute(
            "INSERT INTO turns (task_id, request, status, kind) VALUES (?, ?, 'queued', ?) RETURNING id",
            (task_id, request, kind),
        ).fetchone()["id"]
        self._add_uploads(connection, task_id, turn_id, sources)
        connection.execute(
            "UPDATE tasks SET status = 'queued', response = NULL, error = NULL WHERE id = ?", (task_id,)
        )
        return turn_id

    def _latest_turn(self, connection: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT id, status, request FROM turns WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
        ).fetchone()

    def _expected_turn(
        self, connection: sqlite3.Connection, task_id: str, expected_turn_id: int,
    ) -> sqlite3.Row:
        turn = self._latest_turn(connection, task_id)
        if turn is None:
            raise KeyError(task_id)
        if turn["id"] != expected_turn_id:
            raise ValueError("This page shows an older turn. Reload the assignment and try again.")
        return turn

    def stop_turn(self, task_id: str, expected_turn_id: int) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            turn = self._expected_turn(connection, task_id, expected_turn_id)
            if turn["status"] not in ("queued", "running", "waiting"):
                raise ValueError("Only a queued, running, or waiting turn can be stopped.")
            connection.execute("UPDATE turns SET status = 'stopped' WHERE id = ?", (turn["id"],))
            connection.execute("UPDATE tasks SET status = 'stopped', response = NULL WHERE id = ?", (task_id,))
            self._discard_drafts(connection, turn["id"])

    def correct_turn(self, task_id: str, expected_turn_id: int, request: str) -> int:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            turn = self._expected_turn(connection, task_id, expected_turn_id)
            if turn["status"] != "running":
                raise ValueError("Only a running turn can be corrected.")
            request = request.strip()
            if not request:
                raise ValueError("Enter a correction.")
            connection.execute("UPDATE turns SET status = 'superseded' WHERE id = ?", (turn["id"],))
            self._discard_drafts(connection, turn["id"])
            return self._queue_turn(connection, task_id, request, "correction")

    def continue_turn(self, task_id: str, expected_turn_id: int) -> int:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            turn = self._expected_turn(connection, task_id, expected_turn_id)
            if turn["status"] != "interrupted":
                raise ValueError("Only an interrupted turn can be continued.")
            return self._queue_turn(connection, task_id, turn["request"], "continuation")

    def claim(self) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                UPDATE tasks SET status = 'running'
                WHERE id = (
                    SELECT tasks.id FROM tasks JOIN turns ON turns.task_id = tasks.id
                    WHERE tasks.status = 'queued' AND turns.status = 'queued'
                    ORDER BY turns.id LIMIT 1
                ) AND NOT EXISTS (SELECT 1 FROM tasks WHERE status = 'running')
                RETURNING id
                """
            ).fetchone()
            if row is None:
                return None
            task_id = row["id"]
            self._pin_previous_brief(connection, task_id)
            turn_id = connection.execute(
                "UPDATE turns SET status = 'running' WHERE task_id = ? AND status = 'queued' RETURNING id",
                (task_id,),
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO turn_artifacts (turn_id, name, revision)
                SELECT ?, name, MAX(revision) FROM artifact_revisions WHERE task_id = ? GROUP BY name
                """,
                (turn_id, task_id),
            )
            return task_id

    def get_run(self, task_id: str) -> dict | None:
        with self._connection() as connection:
            connection.execute("BEGIN")
            turns = self._turns(connection, task_id)
            if not turns or turns[-1]["status"] != "running":
                return None
            active = turns[-1]
            messages = []
            request_start = 0
            source_turn = turns[0]
            for previous, following in zip(turns, turns[1:]):
                # A continuation retries the same request with its original attachments.
                if previous["kind"] != "continuation":
                    source_turn = previous
                if previous["kind"] in ("initial", "follow-up"):
                    request_start = len(messages)
                if previous["status"] == "stopped":
                    # Drop this request and its corrections from model context. The
                    # complete sequence stays visible in the assignment history.
                    del messages[request_start:]
                    continue
                if following["kind"] == "continuation":
                    continue
                content = previous["request"]
                if source_turn["kind"] != "initial" and source_turn.get("attached_files"):
                    content = json.dumps({"request": content, "attached_files": source_turn["attached_files"]})
                messages.append({"role": "user", "content": content})
                if previous["status"] in ("completed", "waiting") and previous["response"]:
                    messages.append({"role": "assistant", "content": previous["response"]})
            artifacts = connection.execute(
                """
                SELECT pins.name, pins.revision, saved.author FROM turn_artifacts AS pins
                JOIN artifact_revisions AS saved ON saved.task_id = ? AND saved.name = pins.name
                    AND saved.revision = pins.revision
                WHERE pins.turn_id = ? ORDER BY pins.name
                """,
                (task_id, active["id"]),
            )
            if active["kind"] != "continuation":
                source_turn = active
            return {
                "turn_id": active["id"],
                "request": active["request"],
                "kind": active["kind"],
                "attached_files": source_turn.get("attached_files", []),
                "prior_messages": messages,
                "watch": watches.context(connection, task_id),
                "prior_artifacts": self._prior_artifacts(connection, task_id),
                "artifacts": {
                    row["name"]: {"revision": row["revision"], "author": row["author"]} for row in artifacts
                },
            }

    def _uploads(self, connection: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT name, position, turn_id FROM uploads WHERE task_id = ? ORDER BY position", (task_id,)
        ).fetchall()

    @staticmethod
    def _prepare_uploads(uploads):
        # Parsing can take seconds. Finish before claiming the SQLite writer lock.
        if len(uploads or {}) > MAX_FILES:
            raise ValueError(f"An assignment can have up to {MAX_FILES} uploaded files.")
        sources = []
        for name, data in (uploads or {}).items():
            document = None
            if pdf_documents.is_pdf(data) or name.lower().endswith(".pdf"):
                document = pdf_documents.extract_pdf(data)
            sources.append((name, data, document))
        return sources

    def _add_uploads(self, connection: sqlite3.Connection, task_id: str, turn_id: int, sources) -> None:
        if not sources:
            return
        existing = self._uploads(connection, task_id)
        names = {name for name, _, _ in sources}
        if any(row["name"] in names for row in existing):
            raise ValueError("An upload already uses that filename. Choose a new filename.")
        if len(existing) + len(sources) > MAX_FILES:
            raise ValueError(f"An assignment can have up to {MAX_FILES} uploaded files.")
        start = existing[-1]["position"] + 1 if existing else 0
        for position, (name, data, document) in enumerate(sources, start):
            connection.execute("INSERT INTO uploads(task_id, position, name, content, turn_id) VALUES (?,?,?,?,?)",
                               (task_id, position, name, data, turn_id))
            if document is not None:
                connection.executemany(
                    "INSERT INTO pdf_upload_pages(task_id,name,number,start_line,end_line,text) VALUES(?,?,?,?,?,?)",
                    ((task_id, name, page.number, page.start_line, page.end_line, page.text) for page in document.pages),
                )

    @staticmethod
    def _pdf_layout(connection, task_id, name):
        rows = connection.execute("""SELECT number,start_line,end_line,text != '' AS has_text
            FROM pdf_upload_pages WHERE task_id=? AND name=? ORDER BY number""", (task_id, name))
        return [{**dict(row), "has_text": bool(row["has_text"])} for row in rows]

    def pdf_layouts(self, task_id):
        with self._connection() as db:
            names = db.execute("SELECT DISTINCT name FROM pdf_upload_pages WHERE task_id=?", (task_id,))
            return {name: self._pdf_layout(db, task_id, name) for (name,) in names}

    @staticmethod
    def _pdf_text(connection, task_id, name):
        rows = connection.execute("SELECT text FROM pdf_upload_pages WHERE task_id=? AND name=? ORDER BY number",
                                  (task_id, name)).fetchall()
        if rows:
            return "\n".join(row["text"] for row in rows) + "\n"
        return None

    def read_pdf_view(self, task_id, name, page, overview_line_limit):
        """Load line ranges for navigation and only the text shown in the current view."""
        with self._connection() as db:
            layout = self._pdf_layout(db, task_id, name)
            if not layout or (page is None and layout[-1]["end_line"] > overview_line_limit):
                return layout, {}
            sql = "SELECT number,text FROM pdf_upload_pages WHERE task_id=? AND name=?"
            args = [task_id, name]
            if page is not None:
                sql += " AND number=?"
                args.append(page)
            return layout, {number: text.split("\n") for number, text in db.execute(sql, args)}

    def read_upload_projection(self, task_id, name):
        """Access the immutable extracted text and its original physical page boundaries."""
        with self._connection() as db:
            layout = self._pdf_layout(db, task_id, name)
            if layout:
                return self._pdf_text(db, task_id, name), layout
        return None

    def read_upload_text(self, task_id, name):
        with self._connection() as db:
            text = self._pdf_text(db, task_id, name)
        return text.encode("utf-8") if text is not None else self.read_upload(task_id, name)

    def _upload_bytes(self, task_id: str, entry: sqlite3.Row) -> bytes:
        if entry["content"] is not None:
            return entry["content"]
        # Version 1 stored source bytes on disk under their immutable position.
        return (self._workspace(task_id) / "inputs" / str(entry["position"])).read_bytes()

    def read_uploads(self, task_id: str) -> dict[str, str]:
        with self._connection() as connection:
            result = {}
            for entry in self._uploads(connection, task_id):
                name = entry["name"]
                text = self._pdf_text(connection, task_id, name)
                if text is None:
                    original = connection.execute("SELECT position,content FROM uploads WHERE task_id=? AND name=?",
                                                  (task_id, name)).fetchone()
                    text = self._upload_bytes(task_id, original).decode("utf-8")
                result[name] = text
            return result

    def read_upload(self, task_id: str, name: str) -> bytes | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT position, content FROM uploads WHERE task_id = ? AND name = ?", (task_id, name)
            ).fetchone()
        if row is None:
            return None
        try:
            return self._upload_bytes(task_id, row)
        except FileNotFoundError:
            return None

    def _running_turn(self, connection: sqlite3.Connection, task_id: str, turn_id: int | None) -> int:
        row = self._latest_turn(connection, task_id)
        if row is None or row["status"] != "running" or (turn_id is not None and row["id"] != turn_id):
            raise InactiveTurn("This assignment turn is no longer running.")
        return row["id"]

    def require_running(self, task_id: str, turn_id: int) -> None:
        with self._connection() as connection:
            self._running_turn(connection, task_id, turn_id)

    def _artifact_bytes(self, name: str, content: str) -> bytes:
        if not ARTIFACT_NAME.fullmatch(name):
            raise ValueError("Use a simple filename ending in .csv or .md.")
        data = content.encode("utf-8")
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ValueError("An artifact cannot exceed 1 MiB.")
        return data

    def stage(self, task_id: str, name: str, content: str, *, turn_id: int | None = None) -> None:
        data = self._artifact_bytes(name, content)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = self._running_turn(connection, task_id, turn_id)
            connection.execute(
                """
                INSERT INTO staged_artifacts (turn_id, name, content) VALUES (?, ?, ?)
                ON CONFLICT(turn_id, name) DO UPDATE SET content = excluded.content
                """,
                (active, name, data),
            )

    def stage_method(
        self, task_id: str, title: str, scope: str, procedure: str, evidence: str, *, turn_id: int,
    ) -> None:
        values = self._method_values(title, scope, procedure, evidence)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = self._running_turn(connection, task_id, turn_id)
            connection.execute(
                """
                INSERT INTO staged_methods (turn_id, title, scope, procedure, evidence) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET title = excluded.title, scope = excluded.scope,
                    procedure = excluded.procedure, evidence = excluded.evidence
                """,
                (active, *values),
            )

    def _discard_drafts(self, connection: sqlite3.Connection, turn_id: int) -> None:
        connection.execute("DELETE FROM staged_artifacts WHERE turn_id = ?", (turn_id,))
        connection.execute("DELETE FROM staged_methods WHERE turn_id = ?", (turn_id,))
        connection.execute("DELETE FROM staged_watch_assessments WHERE turn_id = ?", (turn_id,))
        connection.execute("DELETE FROM staged_project_notes WHERE turn_id = ?", (turn_id,))
        connection.execute("DELETE FROM evidence_changes WHERE turn_id=? AND published=0", (turn_id,))

    def progress(self, task_id: str, message: str, *, turn_id: int | None = None) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = self._running_turn(connection, task_id, turn_id)
            connection.execute(
                "INSERT INTO events (task_id, message, turn_id) VALUES (?, ?, ?)", (task_id, message, active)
            )

    def record_step(self, task_id: str, step: dict, *, turn_id: int | None = None) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = self._running_turn(connection, task_id, turn_id)
            cursor = connection.execute(
                "INSERT INTO run_steps (task_id, payload, turn_id) VALUES (?, ?, ?)",
                (task_id, json.dumps(step), active),
            )
            evidence_links.record_read(connection, task_id, active, step, cursor.lastrowid)

    def evidence_uses(self, source_task_id: str, name: str, revision: int | None, offset: int = 0) -> dict:
        with self._connection() as connection:
            pending = revision is None and connection.execute("""SELECT 1 FROM pending_upload_citations
                WHERE source_task_id=? AND source_name=? LIMIT 1""", (source_task_id, name)).fetchone()
            if pending:
                connection.execute("BEGIN IMMEDIATE")
                evidence_links.resolve_pending(connection, self._upload_bytes, source_task_id, name)
            else:
                connection.execute("BEGIN")
            result = evidence_links.uses(connection, source_task_id, name, revision, offset)
        changes = self.evidence_changes(source_task_id, name, revision, offset)
        result["evidence_changes"] = changes["changes"]
        result["next_offset"] = result["next_offset"] or changes["next_offset"]
        return result

    def find_evidence_uses(self, task_id: str, source: int | dict, offset: int = 0) -> dict:
        if type(source) is int:
            pin = self.prior_artifact(task_id, source)
            if pin is None:
                raise ValueError("Choose an earlier file pinned to this assignment.")
            target = pin["source_task_id"], pin["name"], pin["revision"]
        elif isinstance(source, dict) and source.keys() == {"upload"} and isinstance(source["upload"], str):
            target = task_id, source["upload"], None
        elif (isinstance(source, dict) and source.keys() == {"artifact", "revision"}
              and isinstance(source["artifact"], str) and type(source["revision"]) is int
              and 0 < source["revision"] < 2**63):
            target = task_id, source["artifact"], source["revision"]
        else:
            raise ValueError("Choose an original upload, an exact saved revision, or a pinned file.")
        result = self.evidence_uses(*target, offset)
        for row in result["citing_revisions"] + result["indirect_review_candidates"]:
            row["same_assignment"] = row["source_task_id"] == task_id
            if "via" in row:
                row["via"]["same_assignment"] = row["via"]["source_task_id"] == task_id
        return result

    def source_evidence(self, task_id: str, source_ref: str) -> dict | None:
        if not re.fullmatch(r"[a-f0-9]{32}", source_ref):
            return None
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT json_extract(payload, '$.output') FROM run_steps
                WHERE task_id = ? AND json_extract(payload, '$.source_ref') = ?
                    AND json_extract(payload, '$.type') = 'function_call_output'
                """,
                (task_id, source_ref),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def run_history(self, task_id: str) -> dict | None:
        with self._connection() as connection:
            connection.execute("BEGIN")
            task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                return None
            turns = self._turns(connection, task_id)
            by_id = {turn["id"]: turn for turn in turns}
            for turn in turns:
                turn["steps"] = []
            steps = []
            for row in connection.execute(
                "SELECT turn_id, payload FROM run_steps WHERE task_id = ? ORDER BY id", (task_id,)
            ):
                step = json.loads(row["payload"])
                by_id[row["turn_id"]]["steps"].append(step)
                steps.append(step)
            return {
                **dict(task),
                "uploads": [entry["name"] for entry in self._uploads(connection, task_id)],
                "steps": steps,
                "turns": turns,
                "prior_artifacts": self._prior_artifacts(connection, task_id),
            }

    def complete(self, task_id: str, response: str, *, turn_id: int | None = None) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = self._running_turn(connection, task_id, turn_id)
            try:
                watch_outcome = watches.publish(connection, task_id, active)
            except watches.WatchError as exc:
                raise RevisionConflict(str(exc)) from exc
            # Partial observations can explain an outage, but cannot justify
            # publishing staged decisions or procedures based on incomplete evidence.
            if watch_outcome == "unavailable":
                self._discard_drafts(connection, active)
            drafts = connection.execute(
                """
                SELECT draft.name, draft.content, COALESCE(pins.revision, 0) AS expected,
                      (SELECT COALESCE(MAX(revision), 0) FROM artifact_revisions
                         WHERE task_id = ? AND name = draft.name) AS current
                FROM staged_artifacts AS draft
                LEFT JOIN turn_artifacts AS pins ON pins.turn_id = draft.turn_id AND pins.name = draft.name
                WHERE draft.turn_id = ? ORDER BY draft.name
                """,
                (task_id, active),
            ).fetchall()
            for draft in drafts:
                if draft["expected"] != draft["current"]:
                    raise RevisionConflict(
                        f'{draft["name"]} was edited while this turn was running. Open the saved revision and retry.'
                    )
            connection.executemany(
                """
                INSERT INTO artifact_revisions (task_id, name, revision, author, content, turn_id)
                VALUES (?, ?, ?, 'agent', ?, ?)
                """,
                ((task_id, draft["name"], draft["current"] + 1, draft["content"], active) for draft in drafts),
            )
            for draft in drafts:
                evidence_links.index_report(
                    connection, self._upload_bytes, task_id, draft["name"], draft["current"] + 1, draft["content"]
                )
            connection.execute(
                """
                INSERT INTO saved_methods (source_task_id, title, scope, procedure, evidence, updated_at)
                SELECT ?, title, scope, procedure, evidence, ? FROM staged_methods WHERE turn_id = ?
                """,
                (task_id, self._method_timestamp(), active),
            )
            try:
                project_notes.publish_notes(connection, task_id, active)
                connection.execute("UPDATE evidence_changes SET published=1 WHERE turn_id=?", (active,))
            except project_notes.NoteConflict as exc:
                raise RevisionConflict(str(exc)) from exc
            self._discard_drafts(connection, active)
            connection.execute(
                "UPDATE turns SET status = 'completed', response = ? WHERE id = ?", (response, active)
            )
            connection.execute(
                "UPDATE tasks SET status = 'completed', response = ? WHERE id = ?", (response, task_id)
            )

    def fail(self, task_id: str, message: str, *, turn_id: int | None = None) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                active = self._running_turn(connection, task_id, turn_id)
            except InactiveTurn:
                return
            self._discard_drafts(connection, active)
            connection.execute("UPDATE turns SET status = 'failed', error = ? WHERE id = ?", (message, active))
            connection.execute("UPDATE tasks SET status = 'failed', error = ? WHERE id = ?", (message, task_id))

    def artifact_revision(self, task_id: str, name: str, revision: int | None = None) -> dict | None:
        if not ARTIFACT_NAME.fullmatch(name):
            return None
        query = "SELECT revision, author, content FROM artifact_revisions WHERE task_id = ? AND name = ?"
        with self._connection() as connection:
            if revision is None:
                row = connection.execute(query + " ORDER BY revision DESC LIMIT 1", (task_id, name)).fetchone()
            else:
                row = connection.execute(query + " AND revision = ?", (task_id, name, revision)).fetchone()
            return dict(row) if row else None

    def artifact_revisions(self, task_id: str, name: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT revision, author FROM artifact_revisions
                WHERE task_id = ? AND name = ? ORDER BY revision
                """,
                (task_id, name),
            )
            return [dict(row) for row in rows]

    def read_run_artifact(self, task_id: str, turn_id: int, name: str) -> bytes | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT saved.content FROM turn_artifacts AS pins
                JOIN turns ON turns.id = pins.turn_id
                JOIN artifact_revisions AS saved ON saved.task_id = turns.task_id AND saved.name = pins.name
                    AND saved.revision = pins.revision
                WHERE turns.task_id = ? AND pins.turn_id = ? AND pins.name = ?
                """,
                (task_id, turn_id, name),
            ).fetchone()
            return row["content"] if row else None

    def save_markdown(self, task_id: str, name: str, content: str, expected_revision: int) -> int:
        if not name.endswith(".md"):
            raise ValueError("Only Markdown artifacts can be edited.")
        data = self._artifact_bytes(name, content)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT MAX(revision) FROM artifact_revisions WHERE task_id = ? AND name = ?",
                (task_id, name),
            ).fetchone()[0]
            if current is None:
                raise KeyError(name)
            if current != expected_revision:
                raise RevisionConflict("The artifact changed while you were editing it. Reopen its current revision.")
            revision = current + 1
            connection.execute(
                """
                INSERT INTO artifact_revisions (task_id, name, revision, author, content)
                VALUES (?, ?, ?, 'user', ?)
                """,
                (task_id, name, revision, data),
            )
            evidence_links.index_report(connection, self._upload_bytes, task_id, name, revision, data)
            return revision

    def read_artifact(self, task_id: str, name: str) -> bytes | None:
        artifact = self.artifact_revision(task_id, name)
        return artifact["content"] if artifact else None

    def _method_values(self, title: str, scope: str, procedure: str, evidence: str) -> tuple[str, ...]:
        fields = []
        for name, value in (("title", title), ("scope", scope), ("procedure", procedure), ("evidence", evidence)):
            text = value.strip() if isinstance(value, str) else ""
            limit = METHOD_FIELD_LIMITS[name]
            if not text or len(text) > limit or "\x00" in text:
                raise ValueError(f"Enter valid {name} text of up to {limit:,} characters.")
            fields.append(text)
        return tuple(fields)

    def _method_timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def save_method(self, source_task_id: str, title: str, scope: str, procedure: str, evidence: str) -> int:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source = connection.execute("SELECT status FROM tasks WHERE id = ?", (source_task_id,)).fetchone()
            if source is None:
                raise KeyError(source_task_id)
            if source["status"] != "completed":
                raise ValueError("Save a method from a completed assignment.")
            values = self._method_values(title, scope, procedure, evidence)
            return connection.execute(
                """
                INSERT INTO saved_methods (source_task_id, title, scope, procedure, evidence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?) RETURNING id
                """,
                (source_task_id, *values, self._method_timestamp()),
            ).fetchone()["id"]

    def list_methods(self, *, limit: int | None = None) -> list[dict]:
        if limit is not None and (type(limit) is not int or not 0 <= limit < 2**63):
            raise ValueError("A saved method limit must be a nonnegative integer.")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM saved_methods ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit if limit is not None else -1,),
            )
            return [dict(row) for row in rows]

    def relevant_project_memory(self, request: str, context: list[str]) -> dict:
        with self._connection() as connection:
            return memory_context.select(connection, request, context, self.changes_for_note_claims)

    def relevant_methods(self, request: str, context: list[str]) -> list[dict]:
        with self._connection() as connection:
            matches = ranked_methods(connection, request)
            if context:
                current_ids = {method["id"] for method in matches}
                matches.extend(method for method in ranked_methods(connection, " ".join(context))
                               if method["id"] not in current_ids)
            return matches

    def get_method(self, method_id: int) -> dict | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM saved_methods WHERE id = ?", (method_id,)).fetchone()
            return dict(row) if row else None

    def _require_method_revision(self, connection, method_id: int, expected_revision: int) -> None:
        row = connection.execute("SELECT revision FROM saved_methods WHERE id = ?", (method_id,)).fetchone()
        if row is None:
            raise KeyError(method_id)
        if row["revision"] != expected_revision:
            raise RevisionConflict("This method has changed.")

    def update_method(
        self, method_id: int, title: str, scope: str, procedure: str, evidence: str, *, expected_revision: int,
    ) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_method_revision(connection, method_id, expected_revision)
            values = self._method_values(title, scope, procedure, evidence)
            connection.execute(
                """
                UPDATE saved_methods SET title = ?, scope = ?, procedure = ?, evidence = ?, updated_at = ?,
                    revision = revision + 1
                WHERE id = ?
                """,
                (*values, self._method_timestamp(), method_id),
            )

    def remove_method(self, method_id: int, *, expected_revision: int) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_method_revision(connection, method_id, expected_revision)
            connection.execute("DELETE FROM saved_methods WHERE id = ?", (method_id,))
