import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from work_agent.storage import Store


class SavedWorkSearchTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.store.initialize()

    def publish(self, request, name="brief.md", content="notes"):
        task = self.store.submit(request, {})
        self.assertEqual(self.store.claim(), task)
        self.store.stage(task, name, content)
        self.store.complete(task, "Saved")
        return task

    def test_search_indexes_published_content_and_requests_in_other_assignments(self):
        iowa = self.publish("Prepare for meeting at the University of Iowa.", content="private-onlyphrase")
        state = self.publish("Make a general plan.", name="plan.md")
        self.store.follow_up(state, "Prépare the Iowa State engineering briefing.")
        self.assertEqual(self.store.claim(), state)
        self.store.stage(state, "brief.md", "New brief")
        self.store.complete(state, "Saved")
        current = self.publish("My own Iowa State briefing.", "own.md")
        failed = self.store.submit("Iowa State", {})
        self.assertEqual(self.store.claim(), failed)
        self.store.stage(failed, "draft.md", "unpublished")
        self.store.fail(failed, "Offline")

        query = self.store.search_saved_work
        self.assertEqual([r["source_task_id"] for r in query(current, "University Iowa")], [iowa])
        result, = query(current, 'state:briefing "prepare"')
        self.assertEqual((result["source_task_id"], result["name"], result["revision"]), (state, "brief.md", 1))
        self.assertIn("Prépare", result["request"])
        self.assertEqual(query(current, "private-onlyphrase")[0]["passage"]["text"], "private-onlyphrase")
        self.assertEqual(query(current, "draft.md"), [])
        self.assertEqual(self.store.get_task(current)["prior_artifacts"], [])
        for invalid in (None, "", '"-*', "a" * 201):
            with self.assertRaises(ValueError):
                query(current, invalid)

    def test_exact_filename_limits_and_disambiguation(self):
        target = self.publish("At the farmer's market", "purchases.csv")
        for number in range(9):
            self.publish(f"Summarize purchases.csv for revision {number}.", f"summary-{number}.csv")
        current = self.store.submit("Find my purchases file.", {})
        hits = self.store.search_saved_work(current, "PURCHASES.csv")
        self.assertEqual(len(hits), 8)
        self.assertEqual((hits[0]["source_task_id"], hits[0]["name"]), (target, "purchases.csv"))
        self.assertEqual(hits, self.store.search_saved_work(current, "PURCHASES.csv"))

    def test_selected_revision_remains_exact_across_edit_restart_and_repeated_selection(self):
        source = self.publish("Original brief", content="Revision one")
        current = self.store.submit("Work from that brief.", {})
        result, = self.store.search_saved_work(current, "brief.md")
        self.store.save_markdown(source, "brief.md", "Revision two", 1)
        def pin(_):
            return self.store.pin_saved_work(current, result["source_task_id"], result["name"], result["revision"])

        with ThreadPoolExecutor(2) as executor:
            first, second = executor.map(pin, (0, 1))
        self.assertEqual(first, second)
        self.store.initialize()
        ref = first["reference_id"]
        self.assertEqual(self.store.get_task(current)["prior_artifacts"], [first])
        self.assertEqual(self.store.read_prior_artifact(current, ref), b"Revision one")
        self.assertEqual(self.store.search_saved_work(current, "brief.md")[0]["revision"], 2)
        for source_id, revision in ((current, 1), (source, True), (source, 99), ("missing", 1)):
            with self.assertRaises(ValueError):
                self.store.pin_saved_work(current, source_id, "brief.md", revision)

    def test_search_backfills_an_existing_volume_without_duplicating_user_revisions(self):
        task = self.publish("Résumé and research brief")
        self.store.save_markdown(task, "brief.md", "User edits", 1)
        with self.store._connection() as connection:
            connection.execute("DROP TRIGGER index_published_artifact")
            connection.execute("DROP TABLE saved_work_search")
        self.store.initialize()
        self.store.initialize()
        current = self.store.submit("Get earlier work.", {})
        hit, = self.store.search_saved_work(current, "resume")
        self.assertEqual((hit["source_task_id"], hit["revision"]), (task, 2))
