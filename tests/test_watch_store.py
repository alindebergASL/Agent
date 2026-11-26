import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

from work_agent.storage import InactiveTurn, RevisionConflict, Store


NOW = datetime(2026, 10, 30, 15, tzinfo=UTC)
SOURCE = "https://example.org/bulletin/123"


class WatchStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.folder)
        self.store.initialize()
        self.watch = self.store.create_watch("Watch for marine research calls.", "daily", None, "09:00", now=NOW)

    def run_now(self):
        task = self.store.enqueue_watch_now(self.watch, now=NOW)
        self.assertEqual(self.store.claim(), task)
        return task, self.store.get_run(task)["turn_id"]

    def stage(self, task, turn, outcome, keys=(), summary="New observation"):
        self.store.stage_watch_assessment(
            task, turn, outcome=outcome, summary=summary, evidence=[SOURCE] if outcome != "unavailable" else [],
            findings=[{"key": key, "title": key, "why": "The opportunity meets the eligibility criteria.",
                       "evidence": [SOURCE]} for key in keys],
        )

    def test_daily_weekly_and_existing_schedule_coexist_and_catch_up_once_across_dst(self):
        weekly = self.store.create_watch("Check contract decisions.", "weekly", 4, "09:00", now=NOW)
        responsibility = self.store.create_schedule("Weekly research brief", 4, "09:00", now=NOW)
        after_fall_back = datetime(2026, 11, 8, 18, tzinfo=UTC)
        barrier = Barrier(2)

        def poll(_):
            barrier.wait(timeout=5)
            return Store(self.folder).enqueue_due_watches(now=after_fall_back)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(poll, range(2)))
        self.assertEqual(sorted(map(len, results)), [0, 2])
        for watch_id, date in ((self.watch, 8), (weekly, 6)):
            w = self.store.get_watch(watch_id)
            self.assertEqual(w["checks"][0]["due_at"], int(datetime(2026, 11, date, 17, tzinfo=UTC).timestamp()))
        self.assertEqual(self.store.get_watch(self.watch)["next_run_at"],
                         int(datetime(2026, 11, 9, 17, tzinfo=UTC).timestamp()))
        self.assertEqual(self.store.get_watch(weekly)["next_run_at"],
                         int(datetime(2026, 11, 13, 17, tzinfo=UTC).timestamp()))
        self.assertIsNotNone(self.store.enqueue_due_schedule(now=after_fall_back))
        self.store.stop_watch(self.watch, now=after_fall_back)
        self.store.initialize()
        self.assertEqual(self.store.enqueue_due_watches(now=after_fall_back), [])
        with self.assertRaisesRegex(ValueError, "stopped"):
            self.store.enqueue_watch_now(self.watch, now=after_fall_back)
        self.assertEqual(self.store.get_schedule()["id"], responsibility)
        self.assertIsNone(self.store.get_schedule()["stopped_at"])

    def test_findings_are_atomic_distinct_per_watch_and_acknowledged_individually(self):
        task, turn = self.run_now()
        initial_due = self.store.get_watch(self.watch)["next_run_at"]
        self.assertEqual(self.store.enqueue_watch_now(self.watch, now=NOW), task)
        self.assertEqual(self.store.get_watch(self.watch)["next_run_at"], initial_due)
        self.stage(task, turn, "material", ["award-7", "rule-9"])
        self.assertEqual(self.store.watch_attention(), [])
        self.store.complete(task, "Two changes", turn_id=turn)
        notices = self.store.watch_attention()
        self.assertEqual({row["key"] for row in notices}, {"award-7", "rule-9"})
        self.store.acknowledge_watch_notice(self.watch, notices[0]["id"])
        self.store.initialize()
        self.assertEqual(self.store.watch_attention()[0]["id"], notices[1]["id"])

        task2, turn2 = self.run_now()
        context = self.store.get_run(task2)["watch"]
        self.assertEqual({event["key"] for event in context["known_events"]}, {"award-7", "rule-9"})
        self.assertEqual(context["previous"]["task_id"], task)
        self.stage(task2, turn2, "material", ["AWARD-7", "new-award"])
        self.store.complete(task2, "One new item", turn_id=turn2)
        self.assertEqual(len(self.store.watch_attention()), 2)
        self.assertEqual([f["key"] for f in self.store.get_watch(self.watch)["checks"][0]["findings"]],
                         ["new-award"])

        third, turn3 = self.run_now()
        self.stage(third, turn3, "material", ["new-award"])
        self.store.complete(third, "Saw the same award", turn_id=turn3)
        self.assertEqual(self.store.get_watch(self.watch)["checks"][0]["outcome"], "unchanged")
        other = self.store.create_watch("Other independent rule watch", "daily", None, "12:30", now=NOW)
        fresh = self.store.enqueue_watch_now(other, now=NOW)
        self.assertEqual(self.store.claim(), fresh)
        self.assertEqual(self.store.get_run(fresh)["watch"]["known_events"], [])
        self.stage(fresh, self.store.get_run(fresh)["turn_id"], "material", ["new-award"])
        self.store.complete(fresh, "Matches another standing request.")
        self.assertEqual(len(self.store.watch_attention()), 3)
        self.store.follow_up(task, "Explain the change.")
        self.assertEqual(self.store.claim(), task)
        self.assertIsNone(self.store.get_run(task)["watch"])
        self.store.complete(task, "The relevant policy changed.")
        self.assertEqual(len(self.store.watch_attention()), 3)

    def test_due_occurrence_is_not_lost_while_a_check_waits_for_information(self):
        waiting, turn = self.run_now()
        self.store.wait_for_input(waiting, "Which catalog?", turn_id=turn)
        later = NOW + timedelta(days=4)
        self.assertEqual(self.store.enqueue_due_watches(now=later), [])
        self.assertLessEqual(self.store.get_watch(self.watch)["next_run_at"], later.timestamp())
        self.store.answer(waiting, turn, "Use the official catalog.")
        self.assertEqual(self.store.claim(), waiting)
        active = self.store.get_run(waiting)["turn_id"]
        self.stage(waiting, active, "baseline")
        self.store.complete(waiting, "Recorded.", turn_id=active)
        follow, = self.store.enqueue_due_watches(now=later)
        self.assertNotEqual(follow, waiting)
        self.assertEqual(self.store.enqueue_due_watches(now=later), [])

    def test_unavailable_check_only_publishes_outage_history(self):
        original = self.store.submit("Save the published criterion.", {"terms.txt": b"Only accredited suppliers."})
        self.assertEqual(self.store.claim(), original)
        turn = self.store.get_run(original)["turn_id"]
        claim = {"text": "Only accredited suppliers.", "sources": ["../uploads/terms.txt#L1"]}
        self.store.stage_project_note(original, turn, "Supplier accreditation", 0, [claim])
        self.store.complete(original, "Saved.")
        before = self.store.read_project_note("Supplier accreditation")

        task, turn = self.run_now()
        provisional = {"text": "The accreditor changed its rules.", "sources": before["claims"][0]["sources"]}
        self.store.stage_project_note(task, turn, before["scope"], 1, [provisional], reason="Awaiting final source")
        self.store.stage(task, "verdict.md", "Draft based on partial information.", turn_id=turn)
        self.store.stage_method(task, "New procedure", "Supplier eligibility", "Use the changed criterion",
                                "Incomplete", turn_id=turn)
        self.stage(task, turn, "unavailable", summary="The accreditor could not be reached.")
        self.store.complete(task, "Source unavailable.", turn_id=turn)
        self.assertEqual(self.store.get_watch(self.watch)["checks"][0]["outcome"], "unavailable")
        self.assertEqual(self.store.read_project_note(before["scope"]), before)
        self.assertIsNone(self.store.read_artifact(task, "verdict.md"))
        self.assertIsNone(self.store.get_method(1))
        self.assertEqual(self.store.watch_attention(), [])

    def test_failed_unavailable_and_interrupted_checks_never_replace_baseline(self):
        first, turn = self.run_now()
        self.stage(first, turn, "baseline", summary="Original permit rule.")
        self.store.complete(first, "Recorded.", turn_id=turn)
        unavailable, turn = self.run_now()
        self.stage(unavailable, turn, "unavailable", summary="Source offline.")
        self.store.complete(unavailable, "Unavailable.", turn_id=turn)
        pending, turn = self.run_now()
        context = self.store.get_run(pending)["watch"]
        self.assertEqual(context["previous"]["task_id"], first)
        self.assertEqual(context["baseline"]["summary"], "Original permit rule.")
        self.assertEqual(context["previous_attempt"]["outcome"], "unavailable")
        with self.assertRaises(RevisionConflict):
            self.store.complete(pending, "Missing assessment.", turn_id=turn)
        self.stage(pending, turn, "material", ["invalidated"])
        self.store.initialize()
        with self.assertRaises(InactiveTurn):
            self.store.complete(pending, "stale", turn_id=turn)
        retry = self.store.continue_turn(pending, turn)
        self.assertEqual(self.store.claim(), pending)
        with self.assertRaises(RevisionConflict):
            self.store.complete(pending, "Staged assessment cannot survive interruption.", turn_id=retry)
        self.stage(pending, retry, "material", ["invalidated"])
        self.store.correct_turn(pending, retry, "Hold, check a separate source.")
        self.assertEqual(self.store.claim(), pending)
        self.store.fail(pending, "No source access.")
        self.assertEqual(self.store.watch_attention(), [])
        last, _ = self.run_now()
        context = self.store.get_run(last)["watch"]
        self.assertEqual(context["previous"]["task_id"], first)
        self.assertEqual(context["previous_attempt"]["status"], "failed")
        self.assertEqual(context["previous_attempt"]["error"], "No source access.")
