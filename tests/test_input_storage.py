import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from work_agent.storage import MAX_FILES, InactiveTurn, Store


class InputStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root)

    def test_repeated_questions_preserve_context_and_withhold_drafts(self):
        self.store.initialize()
        task = self.store.submit("Reconcile these files.", {"old.csv": b"old\r\n"})
        self.assertEqual(self.store.claim(), task)
        first = self.store.get_run(task)["turn_id"]
        self.store.stage(task, "draft.md", "unverified", turn_id=first)
        self.store.stage_method(task, "Draft", "Scope", "Procedure", "Evidence", turn_id=first)
        self.store.wait_for_input(task, "Which source?", turn_id=first)
        self.assertIsNone(self.store.claim())
        other = self.store.submit("Another assignment.", {})
        self.assertEqual(self.store.claim(), other)
        self.store.complete(other, "Done.")

        self.store = Store(self.root)
        self.store.initialize()
        self.assertEqual(self.store.get_task(task)["status"], "waiting")
        self.assertEqual(self.store.get_task(task)["response"], "Which source?")
        self.assertIsNone(self.store.read_artifact(task, "draft.md"))
        self.assertEqual(self.store.list_methods(), [])
        second = self.store.answer(task, first, "See the attachment.", {"new.csv": b"new\r\n"})
        self.assertEqual(self.store.claim(), task)
        self.assertEqual(self.store.get_run(task)["attached_files"], ["new.csv"])
        with self.assertRaises(InactiveTurn):
            self.store.wait_for_input(task, "Stale question?", turn_id=first)
        with self.assertRaises(InactiveTurn):
            self.store.complete(task, "Stale result.", turn_id=first)
        self.store.wait_for_input(task, "Report in which currency?", turn_id=second)
        self.store.answer(task, second, "USD.")
        self.assertEqual(self.store.claim(), task)
        attached_answer = {"request": "See the attachment.", "attached_files": ["new.csv"]}
        self.assertEqual(self.store.get_run(task)["prior_messages"], [
            {"role": "user", "content": "Reconcile these files."},
            {"role": "assistant", "content": "Which source?"},
            {"role": "user", "content": json.dumps(attached_answer)},
            {"role": "assistant", "content": "Report in which currency?"},
        ])
        self.assertEqual(self.store.get_run(task)["request"], "USD.")
        self.assertEqual(self.store.get_run(task)["attached_files"], [])
        self.assertEqual(self.store.read_uploads(task), {"old.csv": "old\r\n", "new.csv": "new\r\n"})

    def test_question_accepts_one_concurrent_answer_and_its_sources(self):
        self.store.initialize()
        task = self.store.submit("Reconcile.", {})
        self.store.claim()
        first = self.store.get_run(task)["turn_id"]
        self.store.wait_for_input(task, "Which source?", turn_id=first)
        ready = Barrier(2)

        def answer(name):
            ready.wait(timeout=5)
            try:
                self.store.answer(task, first, name, {name: name.encode()})
                return True
            except ValueError:
                return False

        names = ["a.csv", "b.csv"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            accepted = list(executor.map(answer, names))
        self.assertEqual(accepted.count(True), 1)
        winner = names[accepted.index(True)]
        self.assertEqual(self.store.read_uploads(task), {winner: winner})
        turns = self.store.run_history(task)["turns"]
        self.assertEqual([(turn["kind"], turn["status"]) for turn in turns], [
            ("initial", "waiting"), ("answer", "queued"),
        ])
        self.assertEqual(turns[-1]["request"], winner)
        self.assertEqual(turns[-1]["attached_files"], [winner])

    def test_rejected_sources_never_replace_an_upload_or_queue_a_turn(self):
        self.store.initialize()
        sources = {f"{i}.csv": b"original" for i in range(MAX_FILES - 1)}
        task = self.store.submit("Reconcile.", sources)
        self.store.claim()
        first = self.store.get_run(task)["turn_id"]
        self.store.wait_for_input(task, "Which file?", turn_id=first)
        with self.assertRaises(ValueError):
            self.store.answer(task, first, "Use these.", {"new.csv": b"new", "0.csv": b"overwritten"})
        with self.assertRaises(ValueError):
            self.store.answer(task, first, "Use these.", {"one.csv": b"one", "two.csv": b"two"})
        self.assertEqual(self.store.get_task(task)["status"], "waiting")
        self.store.stop_turn(task, first)
        with self.assertRaises(ValueError):
            self.store.answer(task, first, "Too late.", {"late.csv": b"late"})
        self.assertEqual(self.store.get_task(task)["turns"][0]["response"], "Which file?")
        self.assertIsNone(self.store.get_task(task)["response"])
        with self.assertRaises(ValueError):
            self.store.follow_up(task, "New analysis.", {"0.csv": b"changed"})
        self.store.follow_up(task, "New analysis.", {"new.csv": b"new"})
        self.assertEqual(self.store.get_task(task)["uploads"], [*sources, "new.csv"])
        self.assertEqual(self.store.read_upload(task, "0.csv"), b"original")
        turns = self.store.get_task(task)["turns"]
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[1]["attached_files"], ["new.csv"])

    def test_deployed_check_constraints_migrate_without_reclassifying_turns(self):
        statuses = "'queued','running','completed','failed','interrupted','stopped','superseded'"
        with sqlite3.connect(self.root / "agent.sqlite3") as connection:
            connection.executescript(f"""
                PRAGMA user_version = 1;
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT CHECK (status IN ({statuses})),
                    created_at TEXT NOT NULL, response TEXT, error TEXT
                );
                CREATE TABLE turns (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), request TEXT NOT NULL,
                    status TEXT CHECK (status IN ({statuses})),
                    kind TEXT CHECK (kind IN ('initial','follow-up','correction','continuation')),
                    created_at TEXT NOT NULL, response TEXT, error TEXT
                );
                CREATE TABLE uploads (
                    task_id TEXT NOT NULL REFERENCES tasks(id), position INTEGER NOT NULL, name TEXT NOT NULL,
                    PRIMARY KEY (task_id, position), UNIQUE (task_id, name)
                );
                INSERT INTO tasks VALUES ('old', 'Analyze.', 'queued', 'one', NULL, NULL);
                INSERT INTO turns VALUES (2, 'old', 'Analyze.', 'superseded', 'initial', 'one', NULL, NULL);
                INSERT INTO turns VALUES (9, 'old', 'Compare.', 'queued', 'correction', 'two', NULL, NULL);
                INSERT INTO uploads VALUES ('old', 0, 'old.csv');
            """)
        inputs = self.root / "runs" / "old" / "inputs"
        inputs.mkdir(parents=True)
        (inputs / "0").write_bytes(b"legacy\r\n")
        self.store.initialize()
        self.assertEqual(self.store.claim(), "old")
        self.store.wait_for_input("old", "Which source?", turn_id=9)
        self.store.initialize()
        answer = self.store.answer("old", 9, "Use the attachment.", {"new.csv": b"fresh\r\n"})
        self.assertEqual([turn["kind"] for turn in self.store.get_task("old")["turns"]], [
            "initial", "correction", "answer",
        ])
        self.assertEqual(self.store.read_uploads("old"), {"old.csv": "legacy\r\n", "new.csv": "fresh\r\n"})
        self.assertEqual(self.store.claim(), "old")
        self.store.initialize()
        self.store.continue_turn("old", answer)
        self.assertEqual(self.store.claim(), "old")
        self.assertEqual(self.store.get_run("old")["attached_files"], ["new.csv"])
        turns = self.store.run_history("old")["turns"]
        self.assertEqual([turn.get("attached_files", []) for turn in turns], [
            ["old.csv"], [], ["new.csv"], [],
        ])
        with sqlite3.connect(self.root / "agent.sqlite3") as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
