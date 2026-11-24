import json
import re
import shutil
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path


ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.(csv|md)")
MAX_ARTIFACT_BYTES = 1024 * 1024
INTERRUPTED = "The worker restarted before this assignment finished."

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    request TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','interrupted')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    response TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS uploads (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (task_id, position),
    UNIQUE (task_id, name)
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    request TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','interrupted')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    response TEXT,
    error TEXT
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
"""


class RevisionConflict(RuntimeError):
    """A saved artifact changed after the caller read it."""


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
            connection.executescript(SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
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
                INSERT INTO turns (task_id, request, status, created_at, response, error)
                VALUES (?, ?, ?, ?, ?, ?) RETURNING id
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
                    "INSERT INTO turns (task_id, request, status) VALUES (?, ?, 'queued')",
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
                "SELECT id, request, status, created_at, response, error FROM turns WHERE task_id = ? ORDER BY id",
                (task_id,),
            )
        ]

    def _can_follow_up(self, connection: sqlite3.Connection, task_id: str, status: str) -> bool:
        if status == "completed":
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
            turn_id = connection.execute(
                "INSERT INTO turns (task_id, request, status) VALUES (?, ?, 'queued') RETURNING id",
                (task_id, request),
            ).fetchone()["id"]
            connection.execute(
                "UPDATE tasks SET status = 'queued', response = NULL, error = NULL WHERE id = ?", (task_id,)
            )
            return turn_id

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
            for previous in turns[:-1]:
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
        row = connection.execute(
            "SELECT id, status FROM turns WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
        ).fetchone()
        if row is None or row["status"] != "running" or (turn_id is not None and row["id"] != turn_id):
            raise RuntimeError("This assignment turn is no longer running.")
        return row["id"]

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
            connection.execute("DELETE FROM staged_artifacts WHERE turn_id = ?", (active,))
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
            except RuntimeError:
                return
            connection.execute("DELETE FROM staged_artifacts WHERE turn_id = ?", (active,))
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
