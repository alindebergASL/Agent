import sqlite3
import tempfile
import unittest
from pathlib import Path

from work_agent.storage import SCHEMA, InactiveTurn, RevisionConflict, Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root)

    def test_publication_conflict_keeps_user_revision_and_rejects_entire_batch(self):
        self.store.initialize()
        task_id = self.store.submit("Make a report.", {"sample.txt": b"source\r\n"})
        self.assertEqual(self.store.claim(), task_id)
        first = self.store.get_run(task_id)["turn_id"]
        self.store.stage(task_id, "report.md", "First report.", turn_id=first)
        self.store.complete(task_id, "Report saved.", turn_id=first)
        second = self.store.follow_up(task_id, "Update the report.")
        self.assertEqual(self.store.claim(), task_id)
        run = self.store.get_run(task_id)
        self.assertEqual(run["turn_id"], second)
        self.assertEqual(run["prior_messages"], [
            {"role": "user", "content": "Make a report."},
            {"role": "assistant", "content": "Report saved."},
        ])
        self.assertEqual(run["artifacts"], {"report.md": {"revision": 1, "author": "agent"}})

        self.assertEqual(self.store.save_markdown(task_id, "report.md", "My edit.", 1), 2)
        with self.assertRaises(RevisionConflict):
            self.store.save_markdown(task_id, "report.md", "Stale editor content.", 1)
        self.assertEqual(self.store.read_artifact(task_id, "report.md"), b"My edit.")
        self.assertEqual(self.store.read_run_artifact(task_id, second, "report.md"), b"First report.")
        self.assertEqual(self.store.read_upload(task_id, "sample.txt"), b"source\r\n")
        with self.assertRaises(RuntimeError):
            self.store.stage(task_id, "report.md", "Old turn result.", turn_id=first)
        self.store.fail(task_id, "Old turn failure.", turn_id=first)
        self.store.stage(task_id, "report.md", "Replace report.", turn_id=second)
        self.store.stage(task_id, "appendix.csv", "row\nexample\n", turn_id=second)
        with self.assertRaises(RevisionConflict) as error:
            self.store.complete(task_id, "Updated.", turn_id=second)
        self.store.fail(task_id, str(error.exception), turn_id=second)
        self.assertIsNone(self.store.read_artifact(task_id, "appendix.csv"))
        self.assertEqual(self.store.read_artifact(task_id, "report.md"), b"My edit.")
        self.assertEqual(self.store.artifact_revision(task_id, "report.md", 1)["content"], b"First report.")

        third = self.store.follow_up(task_id, "Try again and keep my edit.")
        self.assertEqual(self.store.claim(), task_id)
        self.assertEqual(self.store.get_run(task_id)["prior_messages"][-1], {
            "role": "user", "content": "Update the report.",
        })
        self.assertEqual(self.store.read_run_artifact(task_id, third, "report.md"), b"My edit.")
        self.store.stage(task_id, "report.md", "My edit. Additional details.", turn_id=third)
        self.store.complete(task_id, "Updated.", turn_id=third)
        self.assertEqual(self.store.artifact_revisions(task_id, "report.md"), [
            {"revision": 1, "author": "agent"},
            {"revision": 2, "author": "user"},
            {"revision": 3, "author": "agent"},
        ])

    def test_old_volume_migrates_completed_artifacts_and_interrupted_runs(self):
        with sqlite3.connect(self.root / "agent.sqlite3") as connection:
            connection.executescript("""
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, response TEXT, error TEXT
                );
                CREATE TABLE run_steps (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), payload TEXT NOT NULL
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), message TEXT NOT NULL
                );
                INSERT INTO tasks VALUES ('old', 'Make a report.', 'completed', 'old time', 'Saved.', NULL);
                INSERT INTO tasks VALUES ('inflight', 'Working.', 'running', 'old time', NULL, NULL);
                INSERT INTO run_steps (task_id, payload) VALUES ('old', '{"phase":"work"}');
                INSERT INTO events (task_id, message) VALUES ('old', 'Saved an artifact.');
            """)
        completed = self.root / "runs" / "old" / "outputs"
        completed.mkdir(parents=True)
        (completed / "report.md").write_bytes(b"Legacy\r\n")
        inflight = self.root / "runs" / "inflight" / "outputs"
        inflight.mkdir(parents=True)
        (inflight / "report.md").write_bytes(b"Unpublished\n")

        self.store.initialize()
        old = self.store.get_task("old")
        self.store.initialize()
        self.assertEqual(self.store.get_task("old"), old)
        self.assertEqual(old["events"], ["Saved an artifact."])
        self.assertEqual(self.store.run_history("old")["turns"][0]["steps"], [{"phase": "work"}])
        self.assertEqual(self.store.artifact_revision("old", "report.md"), {
            "revision": 1, "author": "agent", "content": b"Legacy\r\n",
        })
        self.assertTrue(old["can_follow_up"])
        self.assertEqual(self.store.get_task("inflight")["turns"][0]["status"], "interrupted")
        self.assertIsNone(self.store.read_artifact("inflight", "report.md"))

    def test_continuity_volume_retains_foreign_keys_and_supports_turn_controls(self):
        with sqlite3.connect(self.root / "agent.sqlite3") as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript("""
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','interrupted')),
                    created_at TEXT NOT NULL, response TEXT, error TEXT
                );
                CREATE TABLE turns (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    request TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','interrupted')),
                    created_at TEXT NOT NULL, response TEXT, error TEXT
                );
            """)
            connection.executescript(SCHEMA)
            connection.executescript("""
                INSERT INTO tasks VALUES ('old', 'Make a report.', 'running', 'one', NULL, NULL);
                INSERT INTO turns VALUES (7, 'old', 'Make a report.', 'completed', 'one', 'Saved.', NULL);
                INSERT INTO turns VALUES (8, 'old', 'Add one row.', 'running', 'two', NULL, NULL);
                INSERT INTO uploads (task_id, position, name) VALUES ('old', 0, 'source.csv');
                INSERT INTO run_steps VALUES (1, 'old', '{"phase":"work"}', 8);
                INSERT INTO events VALUES (1, 'old', 'Read source.', 8);
                INSERT INTO artifact_revisions VALUES ('old', 'report.md', 1, 'agent', x'610d0a', 7);
                INSERT INTO artifact_revisions VALUES ('old', 'report.md', 2, 'user', x'620d0a', NULL);
                INSERT INTO turn_artifacts VALUES (8, 'report.md', 2);
                INSERT INTO staged_artifacts VALUES (8, 'report.md', x'630d0a');
            """)
        inputs = self.root / "runs" / "old" / "inputs"
        inputs.mkdir(parents=True)
        (inputs / "0").write_bytes(b"id\r\n00427\r\n")

        self.store.initialize()
        self.store.initialize()
        history = self.store.run_history("old")
        self.assertEqual([(turn["id"], turn["kind"], turn["status"]) for turn in history["turns"]], [
            (7, "initial", "completed"), (8, "follow-up", "interrupted"),
        ])
        self.assertEqual(history["turns"][1]["steps"], [{"phase": "work"}])
        self.assertEqual(self.store.get_task("old")["events"], ["Read source."])
        self.assertEqual(self.store.read_upload("old", "source.csv"), b"id\r\n00427\r\n")
        self.assertEqual(self.store.artifact_revision("old", "report.md", 1)["content"], b"a\r\n")
        self.assertEqual(self.store.read_run_artifact("old", 8, "report.md"), b"b\r\n")
        with sqlite3.connect(self.root / "agent.sqlite3") as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("SELECT * FROM staged_artifacts").fetchall(), [])

        continued = self.store.continue_turn("old", 8)
        self.assertEqual(self.store.claim(), "old")
        self.assertEqual(self.store.get_run("old")["request"], "Add one row.")
        self.assertEqual(self.store.get_run("old")["prior_messages"], [
            {"role": "user", "content": "Make a report."},
            {"role": "assistant", "content": "Saved."},
        ])
        corrected = self.store.correct_turn("old", continued, "Add two rows.")
        with self.assertRaises(ValueError):
            self.store.stop_turn("old", continued)
        with self.assertRaises(InactiveTurn):
            self.store.complete("old", "Late result.", turn_id=continued)
        self.store.stop_turn("old", corrected)
        self.assertIsNone(self.store.claim())
        self.assertEqual([turn["status"] for turn in self.store.get_task("old")["turns"]], [
            "completed", "interrupted", "superseded", "stopped",
        ])
        self.assertEqual(self.store.read_artifact("old", "report.md"), b"b\r\n")
