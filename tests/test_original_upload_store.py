import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from work_agent.storage import Store


class OriginalUploadTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.data)
        self.store.initialize()

    def test_same_name_in_input_and_output_is_two_distinct_immutable_sources(self):
        original_bytes = b"part,units\r\n00042,12\r\n"
        source = self.store.submit("Summarize parts", {"quote.csv": original_bytes})
        self.store.claim()
        self.store.stage(source, "quote.csv", "part,units\n42,12")
        self.store.complete(source, "Done")
        target = self.store.submit("Find those parts", {})

        hits = self.store.search_saved_work(target, "quote.csv")
        self.assertEqual({(h["kind"], h["revision"]) for h in hits}, {("artifact", 1), ("upload", None)})
        self.assertEqual(self.store.search_saved_work(source, "quote.csv"), [])
        with ThreadPoolExecutor(2) as pool:
            pins = list(pool.map(lambda _: self.store.pin_saved_work(target, source, "quote.csv", None), range(2)))
        self.assertEqual(pins[0], pins[1])
        upload_id = pins[0]["reference_id"]
        artifact_id = self.store.pin_saved_work(target, source, "quote.csv", 1)["reference_id"]
        self.assertNotEqual(upload_id, artifact_id)
        self.store.initialize()
        self.assertEqual(self.store.read_prior_artifact(target, upload_id), original_bytes)
        self.assertEqual(self.store.read_prior_artifact(target, artifact_id), b"part,units\n42,12")
        self.assertIsNone(self.store.read_prior_artifact(source, upload_id))
        self.assertEqual(len(self.store.search_saved_work(target, "quote.csv")), 2)
        with self.store._connection() as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_later_attachment_metadata_is_searchable_even_if_the_turn_fails(self):
        source = self.store.submit("Analyze the order", {})
        self.store.claim()
        self.store.complete(source, "Done")
        name = "supplier #2? café.txt"
        self.store.follow_up(source, "Check freight on the Boston shipment", {name: b"only-inside\r\n"})
        self.store.claim()
        self.store.fail(source, "Source offline")
        target = self.store.submit("Compare", {}, prior_revision=(source, name, None))
        hit, = self.store.search_saved_work(target, "boston cafe")
        self.assertEqual((hit["source_task_id"], hit["kind"], hit["name"]), (source, "upload", name))
        self.assertIn("Boston", hit["request"])
        self.assertEqual(self.store.search_saved_work(target, "only-inside")[0]["passage"]["text"], "only-inside")
        self.assertEqual(self.store.get_task(target)["uploads"], [])
        ref, = self.store.get_task(target)["prior_artifacts"]
        self.assertEqual(self.store.read_prior_artifact(target, ref["reference_id"]), b"only-inside\r\n")
        before = self.store.list_tasks()
        with self.assertRaises(ValueError):
            self.store.submit("Missing upload", {}, prior_revision=(source, "absent.txt", None))
        self.assertEqual(self.store.list_tasks(), before)

    def test_exact_upload_name_with_no_indexable_words(self):
        source = self.store.submit("Figures", {"🍎": b"apples"})
        target = self.store.submit("Compare", {})
        hit, = self.store.search_saved_work(target, "🍎", "upload")
        self.assertEqual((hit["source_task_id"], hit["name"], hit["kind"]), (source, "🍎", "upload"))

    def test_kind_filters_the_full_result_set_before_the_limit(self):
        original = self.store.submit("Quarterly prices", {"prices.csv": b"initial"})
        self.store.claim()
        self.store.complete(original, "Done")
        for i in range(10):
            task = self.store.submit("Quarterly prices", {})
            self.store.claim()
            self.store.stage(task, "prices.csv", str(i))
            self.store.complete(task, "Done")
        target = self.store.submit("Which original?", {})
        search = self.store.search_saved_work
        self.assertEqual(len(search(target, "prices.csv")), 8)
        self.assertTrue(all(hit["kind"] == "artifact" for hit in search(target, "prices.csv")))
        upload, = search(target, "prices.csv", "upload")
        self.assertEqual((upload["source_task_id"], upload["kind"]), (original, "upload"))
        for hit in search(target, "prices.csv", "artifact"):
            self.assertEqual(hit["kind"], "artifact")
        with self.assertRaises(ValueError):
            search(target, "prices.csv", "unknown")

    def test_existing_reference_ids_and_legacy_disk_upload_survive_index_upgrade(self):
        source = self.store.submit("Review old supplies", {"supply.txt": b"legacy\r\n"})
        self.store.claim()
        self.store.stage(source, "brief.md", "Published revision")
        self.store.complete(source, "Done")
        target = self.store.submit("Report", {})
        # Recreate the original artifact-only reference and search tables from PR #14.
        with sqlite3.connect(self.data / "agent.sqlite3") as db:
            db.executescript("""
                DROP TRIGGER index_published_artifact;
                DROP TRIGGER index_original_upload;
                DROP TABLE saved_work_search;
                CREATE VIRTUAL TABLE saved_work_search USING fts5(source_task_id UNINDEXED, name, request);
                DROP TABLE prior_artifacts;
                CREATE TABLE prior_artifacts(
                    reference_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    source_task_id TEXT NOT NULL, name TEXT NOT NULL, revision INTEGER NOT NULL,
                    FOREIGN KEY (source_task_id, name, revision) REFERENCES artifact_revisions(task_id, name, revision),
                    UNIQUE(task_id, source_task_id, name, revision)
                );
            """)
            db.execute("INSERT INTO prior_artifacts VALUES (47, ?, ?, 'brief.md', 1)", (target, source))
            db.execute("UPDATE uploads SET content = NULL, turn_id = NULL WHERE task_id = ?", (source,))
        legacy_folder = self.data / "runs" / source / "inputs"
        legacy_folder.mkdir(parents=True)
        (legacy_folder / "0").write_bytes(b"legacy\r\n")
        self.store.initialize()
        self.store.initialize()
        self.assertEqual(self.store.read_prior_artifact(target, 47), b"Published revision")
        matches = self.store.search_saved_work(target, "supplies")
        self.assertEqual({r["kind"] for r in matches}, {"upload", "artifact"})
        pin = self.store.pin_saved_work(target, source, "supply.txt", None)
        self.assertNotEqual(pin["reference_id"], 47)
        self.assertEqual(self.store.read_prior_artifact(target, pin["reference_id"]), b"legacy\r\n")
        with self.store._connection() as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
