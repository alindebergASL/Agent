import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from work_agent.storage import Store


THURSDAY = datetime(2026, 10, 29, 16, tzinfo=UTC)
FRIDAY = THURSDAY + timedelta(days=1)


class PriorArtifactStorageTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.data)
        self.store.initialize()
        self.schedule_id = self.store.create_schedule("Weekly brief", 4, "09:00", now=THURSDAY)

    def manual(self):
        return self.store.enqueue_schedule_now(self.schedule_id, now=THURSDAY)

    def test_claim_pins_the_published_brief_and_preclaim_edits_across_restart(self):
        original = self.manual()
        self.assertEqual(self.store.claim(), original)
        following = self.store.enqueue_due_schedule(now=FRIDAY)
        self.assertEqual(self.store.get_run(original)["prior_artifacts"], [])
        self.store.stage(original, "brief.md", "Initial brief")
        self.store.stage(original, "appendix.md", "Original appendix")
        self.store.stage(original, "support.csv", "one,two")
        self.store.complete(original, "Saved.")
        self.store.save_markdown(original, "brief.md", "User's corrected brief", 1)

        self.assertEqual(self.store.claim(), following)
        pins = self.store.get_run(following)["prior_artifacts"]
        self.assertEqual([(p["name"], p["revision"], p["author"]) for p in pins], [
            ("appendix.md", 1, "agent"), ("brief.md", 2, "user"),
        ])
        for pin in pins:
            self.assertEqual(pin["source_task_id"], original)
            self.assertEqual(self.store.prior_artifact(following, pin["reference_id"]), pin)
        reference = pins[1]["reference_id"]
        self.store.save_markdown(original, "brief.md", "Subsequent edit", 2)
        self.assertEqual(self.store.read_prior_artifact(following, reference), b"User's corrected brief")

        turn = self.store.get_run(following)["turn_id"]
        self.store = Store(self.data)
        self.store.initialize()
        self.assertEqual(self.store.get_task(following)["prior_artifacts"], pins)
        self.assertEqual(self.store.run_history(following)["prior_artifacts"], pins)
        self.store.continue_turn(following, turn)
        self.assertEqual(self.store.claim(), following)
        self.assertEqual(self.store.get_run(following)["prior_artifacts"], pins)
        self.store.stage(following, "brief.md", "New assignment's own brief")
        self.store.complete(following, "Saved.")
        self.assertEqual(self.store.read_prior_artifact(following, reference), b"User's corrected brief")
        self.assertEqual(self.store.read_artifact(original, "brief.md"), b"Subsequent edit")

    def test_only_the_last_published_brief_of_the_same_responsibility_is_available(self):
        original = self.manual()
        self.assertEqual(self.store.claim(), original)
        self.store.stage(original, "brief.md", "Published brief")
        self.store.complete(original, "Saved.")
        self.store.follow_up(original, "Save independent notes.")
        self.assertEqual(self.store.claim(), original)
        self.store.stage(original, "notes.md", "Independent notes")
        self.store.complete(original, "Saved.")

        failed = self.manual()
        self.assertEqual(self.store.claim(), failed)
        self.store.stage(failed, "draft.md", "Unpublished draft")
        self.store.fail(failed, "Unavailable.")
        other = self.manual()
        self.assertEqual(self.store.claim(), other)
        self.store.stage(other, "inventory.csv", "No brief")
        self.store.complete(other, "Saved inventory.")

        current = self.manual()
        self.assertEqual(self.store.claim(), current)
        pin, = self.store.get_run(current)["prior_artifacts"]
        self.assertEqual((pin["source_task_id"], pin["name"]), (original, "brief.md"))
        ref = pin["reference_id"]
        for invalid in (True, -1, 2**63, str(ref), None):
            self.assertIsNone(self.store.read_prior_artifact(current, invalid))
        self.assertIsNone(self.store.read_prior_artifact(other, ref))
        self.store.complete(current, "Done.")

        one_off = self.store.submit("Separate assignment", {})
        self.store.stop_schedule(self.schedule_id, now=THURSDAY)
        self.schedule_id = self.store.create_schedule("New responsibility", 4, "09:00", now=THURSDAY)
        unrelated = self.manual()
        for task_id in (one_off, unrelated):
            self.assertEqual(self.store.claim(), task_id)
            self.assertEqual(self.store.get_run(task_id)["prior_artifacts"], [])
            self.assertIsNone(self.store.prior_artifact(task_id, ref))
            self.assertIsNone(self.store.read_prior_artifact(task_id, ref))
            self.store.complete(task_id, "Done.")

    def test_explicit_empty_baseline_survives_a_late_earlier_publication(self):
        original = self.manual()
        self.assertEqual(self.store.claim(), original)
        turn = self.store.get_run(original)["turn_id"]
        self.store.wait_for_input(original, "Which source?", turn_id=turn)
        following = self.store.enqueue_due_schedule(now=FRIDAY)
        self.assertEqual(self.store.claim(), following)
        self.assertEqual(self.store.get_run(following)["prior_artifacts"], [])
        self.store.complete(following, "Done.")

        self.store.answer(original, turn, "Use the published source.")
        self.assertEqual(self.store.claim(), original)
        self.store.stage(original, "late.md", "Published after the next run.")
        self.store.complete(original, "Saved.")
        self.store.follow_up(following, "Continue.")
        self.assertEqual(self.store.claim(), following)
        self.assertEqual(self.store.get_run(following)["prior_artifacts"], [])

    def test_late_catchup_follows_a_manual_run_even_with_an_earlier_due_time(self):
        late = FRIDAY + timedelta(minutes=5)
        manual = self.store.enqueue_schedule_now(self.schedule_id, now=late)
        self.assertEqual(self.store.claim(), manual)
        self.store.stage(manual, "brief.md", "First publication")
        self.store.complete(manual, "Saved.")

        catchup = self.store.enqueue_due_schedule(now=late)
        self.assertEqual(self.store.claim(), catchup)
        pin, = self.store.get_run(catchup)["prior_artifacts"]
        self.assertEqual(pin["source_task_id"], manual)
        self.store.stage(catchup, "brief.md", "Next publication")
        self.store.complete(catchup, "Saved.")
        dashboard = self.store.get_schedule()
        self.assertEqual(dashboard["latest_run"]["task_id"], catchup)
        self.assertEqual(dashboard["latest_brief"]["task_id"], catchup)

        following = self.store.enqueue_schedule_now(self.schedule_id, now=late + timedelta(minutes=1))
        self.assertEqual(self.store.claim(), following)
        pin, = self.store.get_run(following)["prior_artifacts"]
        self.assertEqual(pin["source_task_id"], catchup)

    def test_upgrade_preserves_history_and_selects_a_baseline_for_queued_occurrences(self):
        first = self.manual()
        self.assertEqual(self.store.claim(), first)
        self.store.stage(first, "brief.md", "Existing deployment's report.")
        self.store.complete(first, "Saved.")
        old = self.manual()
        self.assertEqual(self.store.claim(), old)
        self.store.complete(old, "No brief.")
        queued = self.manual()
        with self.store._connection() as connection:
            connection.execute("DROP TABLE prior_artifacts")
            connection.execute("ALTER TABLE schedule_occurrences DROP COLUMN prior_resolved")

        self.store.initialize()
        self.assertEqual(self.store.claim(), queued)
        pin, = self.store.get_run(queued)["prior_artifacts"]
        self.assertEqual(pin["source_task_id"], first)
        self.store.complete(queued, "Done.")
        self.store.follow_up(old, "Continue the pre-upgrade assignment.")
        self.assertEqual(self.store.claim(), old)
        self.assertEqual(self.store.get_run(old)["prior_artifacts"], [])
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
