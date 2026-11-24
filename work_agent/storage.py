import json
import re
import shutil
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.(csv|md)")
MAX_ARTIFACT_BYTES = 1024 * 1024
METHOD_FIELD_LIMITS = {"title": 120, "scope": 500, "procedure": 4000, "evidence": 1000}
INTERRUPTED = "The worker restarted before this assignment finished."
SCHEMA_VERSION = 1
STATUSES = "'queued','running','completed','failed','interrupted','stopped','superseded'"

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
    kind TEXT NOT NULL CHECK (kind IN ('initial','follow-up','correction','continuation')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    response TEXT,
    error TEXT
);
"""

SCHEMA = TASKS_TABLE.format(table="tasks") + TURNS_TABLE.format(table="turns") + """
CREATE TABLE IF NOT EXISTS uploads (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (task_id, position),
    UNIQUE (task_id, name)
);
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
"""


class RevisionConflict(RuntimeError):
    """A saved artifact changed after the caller read it."""


class InactiveTurn(RuntimeError):
    """The worker's turn is no longer the active running turn."""


class Store:
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
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            columns = connection.execute("PRAGMA table_info(saved_methods)")
            if "revision" not in {column["name"] for column in columns}:
                connection.execute("ALTER TABLE saved_methods ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
            for table in ("events", "run_steps"):
                columns = connection.execute(f"PRAGMA table_info({table})")
                if "turn_id" not in {column["name"] for column in columns}:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN turn_id INTEGER REFERENCES turns(id)")
            connection.execute("CREATE INDEX IF NOT EXISTS events_turn ON events(turn_id, id)")
            self._migrate_tasks(connection)
            connection.execute(
                "UPDATE turns SET status = 'interrupted', error = ? WHERE status = 'running'",
                (INTERRUPTED,),
            )
            connection.execute(
                "UPDATE tasks SET status = 'interrupted', error = ? WHERE status = 'running'",
                (INTERRUPTED,),
            )
            for table in ("staged_artifacts", "staged_methods"):
                connection.execute(
                    f"DELETE FROM {table} WHERE turn_id IN (SELECT id FROM turns WHERE status = 'interrupted')"
                )
            if version < SCHEMA_VERSION:
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise RuntimeError("Assignment database migration found a broken reference.")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

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
            connection.execute(TURNS_TABLE.format(table="turns_upgrade"))
            connection.execute(
                """
                INSERT INTO turns_upgrade (id, task_id, request, status, created_at, response, error, kind)
                SELECT id, task_id, request, status, created_at, response, error,
                    CASE WHEN id = (
                        SELECT MIN(first.id) FROM turns AS first WHERE first.task_id = turns.task_id
                    ) THEN 'initial' ELSE 'follow-up' END
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

    def submit(self, request: str, uploads: dict[str, bytes]) -> str:
        task_id = uuid.uuid4().hex
        workspace = self._workspace(task_id)
        workspace.mkdir()
        try:
            inputs = workspace / "inputs"
            inputs.mkdir()
            entries = []
            for position, (name, content) in enumerate(uploads.items()):
                (inputs / str(position)).write_bytes(content)
                entries.append((task_id, position, name))
            with self._connection() as connection:
                connection.execute(
                    "INSERT INTO tasks (id, request, status) VALUES (?, ?, 'queued')",
                    (task_id, request),
                )
                connection.execute(
                    "INSERT INTO turns (task_id, request, status, kind) VALUES (?, ?, 'queued', 'initial')",
                    (task_id, request),
                )
                connection.executemany(
                    "INSERT INTO uploads (task_id, position, name) VALUES (?, ?, ?)", entries
                )
        except Exception:
            shutil.rmtree(workspace)
            raise
        return task_id

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
            task["can_follow_up"] = self._can_follow_up(connection, task_id, task["status"])
            return task

    def _turns(self, connection: sqlite3.Connection, task_id: str) -> list[dict]:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, request, status, kind, created_at, response, error
                FROM turns WHERE task_id = ? ORDER BY id
                """,
                (task_id,),
            )
        ]

    def _can_follow_up(self, connection: sqlite3.Connection, task_id: str, status: str) -> bool:
        if status in ("completed", "stopped"):
            return True
        return status == "failed" and connection.execute(
            "SELECT 1 FROM turns WHERE task_id = ? AND status = 'completed' LIMIT 1", (task_id,)
        ).fetchone() is not None

    def follow_up(self, task_id: str, request: str) -> int:
        request = request.strip()
        if not request:
            raise ValueError("Enter a follow-up request.")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if not self._can_follow_up(connection, task_id, row["status"]):
                raise ValueError("This assignment is not ready for a follow-up.")
            return self._queue_turn(connection, task_id, request, "follow-up")

    def _queue_turn(self, connection: sqlite3.Connection, task_id: str, request: str, kind: str) -> int:
        turn_id = connection.execute(
            "INSERT INTO turns (task_id, request, status, kind) VALUES (?, ?, 'queued', ?) RETURNING id",
            (task_id, request, kind),
        ).fetchone()["id"]
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
            if turn["status"] not in ("queued", "running"):
                raise ValueError("Only a queued or running turn can be stopped.")
            connection.execute("UPDATE turns SET status = 'stopped' WHERE id = ?", (turn["id"],))
            connection.execute("UPDATE tasks SET status = 'stopped' WHERE id = ?", (task_id,))
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
            for previous, following in zip(turns, turns[1:]):
                if previous["kind"] in ("initial", "follow-up"):
                    request_start = len(messages)
                if previous["status"] == "stopped":
                    # Drop this request and its corrections from model context. The
                    # complete sequence stays visible in the assignment history.
                    del messages[request_start:]
                    continue
                if following["kind"] == "continuation":
                    continue
                messages.append({"role": "user", "content": previous["request"]})
                if previous["status"] == "completed" and previous["response"]:
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
            return {
                "turn_id": active["id"],
                "request": active["request"],
                "kind": active["kind"],
                "prior_messages": messages,
                "artifacts": {
                    row["name"]: {"revision": row["revision"], "author": row["author"]} for row in artifacts
                },
            }

    def _uploads(self, connection: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT name, position FROM uploads WHERE task_id = ? ORDER BY position", (task_id,)
        ).fetchall()

    def read_uploads(self, task_id: str) -> dict[str, str]:
        with self._connection() as connection:
            entries = self._uploads(connection, task_id)
        inputs = self._workspace(task_id) / "inputs"
        return {
            entry["name"]: (inputs / str(entry["position"])).read_bytes().decode("utf-8")
            for entry in entries
        }

    def read_upload(self, task_id: str, name: str) -> bytes | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT position FROM uploads WHERE task_id = ? AND name = ?", (task_id, name)
            ).fetchone()
        if row is None:
            return None
        return (self._workspace(task_id) / "inputs" / str(row["position"])).read_bytes()

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
            connection.execute(
                "INSERT INTO run_steps (task_id, payload, turn_id) VALUES (?, ?, ?)",
                (task_id, json.dumps(step), active),
            )

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
            }

    def complete(self, task_id: str, response: str, *, turn_id: int | None = None) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = self._running_turn(connection, task_id, turn_id)
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
            connection.execute(
                """
                INSERT INTO saved_methods (source_task_id, title, scope, procedure, evidence, updated_at)
                SELECT ?, title, scope, procedure, evidence, ? FROM staged_methods WHERE turn_id = ?
                """,
                (task_id, self._method_timestamp(), active),
            )
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
