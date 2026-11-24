import json
import re
import shutil
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path


ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.(csv|md)")
MAX_ARTIFACT_BYTES = 1024 * 1024

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
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_steps (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_steps_task ON run_steps(task_id, id);
"""


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
            connection.execute(
                "UPDATE tasks SET status = 'interrupted', error = ? WHERE status = 'running'",
                ("The worker restarted before this assignment finished.",),
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
            task["events"] = [
                event["message"]
                for event in connection.execute(
                    "SELECT message FROM events WHERE task_id = ? ORDER BY id", (task_id,)
                )
            ]
        task["artifacts"] = []
        if task["status"] == "completed":
            outputs = self._workspace(task_id) / "outputs"
            task["artifacts"] = sorted(path.name for path in outputs.iterdir())
        return task

    def claim(self) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                UPDATE tasks SET status = 'running'
                WHERE id = (SELECT id FROM tasks WHERE status = 'queued' ORDER BY rowid LIMIT 1)
                  AND NOT EXISTS (SELECT 1 FROM tasks WHERE status = 'running')
                RETURNING id
                """
            ).fetchone()
            return row["id"] if row else None

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

    def _require_running(self, connection: sqlite3.Connection, task_id: str) -> None:
        row = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["status"] != "running":
            raise RuntimeError("The assignment is no longer running.")

    def stage(self, task_id: str, name: str, content: str) -> None:
        if not ARTIFACT_NAME.fullmatch(name):
            raise ValueError("Use a simple filename ending in .csv or .md.")
        data = content.encode("utf-8")
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ValueError("An artifact cannot exceed 1 MiB.")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_running(connection, task_id)
            workspace = self._workspace(task_id)
            if (workspace / "outputs").exists():
                raise RuntimeError("Publication has already started for this assignment.")
            staged = workspace / "staged"
            staged.mkdir(exist_ok=True)
            temp = workspace / "artifact.tmp"
            temp.write_bytes(data)
            temp.replace(staged / name)

    def progress(self, task_id: str, message: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_running(connection, task_id)
            connection.execute("INSERT INTO events (task_id, message) VALUES (?, ?)", (task_id, message))

    def record_step(self, task_id: str, step: dict) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_running(connection, task_id)
            connection.execute(
                "INSERT INTO run_steps (task_id, payload) VALUES (?, ?)",
                (task_id, json.dumps(step)),
            )

    def run_history(self, task_id: str) -> dict | None:
        with self._connection() as connection:
            connection.execute("BEGIN")
            task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                return None
            steps = connection.execute(
                "SELECT payload FROM run_steps WHERE task_id = ? ORDER BY id", (task_id,)
            )
            return {
                **dict(task),
                "uploads": [entry["name"] for entry in self._uploads(connection, task_id)],
                "steps": [json.loads(step["payload"]) for step in steps],
            }

    def complete(self, task_id: str, response: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_running(connection, task_id)
            workspace = self._workspace(task_id)
            staged = workspace / "staged"
            outputs = workspace / "outputs"
            if not outputs.exists():
                staged.mkdir(exist_ok=True)
                staged.rename(outputs)
            # The web UI only exposes the output directory after this commit.
            connection.execute(
                "UPDATE tasks SET status = 'completed', response = ? WHERE id = ?", (response, task_id)
            )

    def fail(self, task_id: str, message: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE tasks SET status = 'failed', error = ? WHERE id = ? AND status = 'running'",
                (message, task_id),
            )

    def read_artifact(self, task_id: str, name: str) -> bytes | None:
        if not ARTIFACT_NAME.fullmatch(name):
            return None
        with self._connection() as connection:
            published = connection.execute(
                "SELECT 1 FROM tasks WHERE id = ? AND status = 'completed'", (task_id,)
            ).fetchone()
        if published is None:
            return None
        try:
            return (self._workspace(task_id) / "outputs" / name).read_bytes()
        except FileNotFoundError:
            return None
