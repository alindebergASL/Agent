import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_watch_store import NOW, SOURCE
from work_agent import watch_history
from work_agent.storage import Store


class WatchHistoryTests(unittest.TestCase):
    def test_archived_acknowledged_events_searchable_only_in_their_original_watch(self):
        store = Store(Path(self.enterContext(TemporaryDirectory())))
        store.initialize()
        left, right = [store.create_watch(label, "daily", None, "12:00", now=NOW) for label in ("West", "East")]
        def publish(watch, keys):
            task = store.enqueue_watch_now(watch, now=NOW)
            self.assertEqual(store.claim(), task)
            store.stage_watch_assessment(task, store.get_run(task)["turn_id"], outcome="material",
                summary="Published awards", evidence=[SOURCE], findings=[
                    {"key": key, "title": f"Award {key}", "why": "Eligibility changed.", "evidence": [SOURCE]}
                    for key in keys])
            store.complete(task, "Published.")
            return task
        first = publish(left, ["ARCHIVE-907"])
        store.acknowledge_watch_notice(left, store.watch_attention()[0]["id"])
        publish(right, ["ARCHIVE-907"])
        for start in (0, 15):
            publish(left, [f"News-{i}" for i in range(start, start + 15)])
        check = store.enqueue_watch_now(left, now=NOW)
        found = watch_history.search(store, check, "Archive-907", 0)
        self.assertEqual([r["task_id"] for r in found["findings"]], [first])
        self.assertEqual(found["findings"][0]["evidence"], [SOURCE])
        page = watch_history.search(store, check, "News", 0)
        second = watch_history.search(store, check, "News", page["next_offset"])
        self.assertFalse({f["key"] for f in page["findings"]} & {f["key"] for f in second["findings"]})
        self.assertEqual(watch_history.search(store, first, "News", 0)["findings"], [])
