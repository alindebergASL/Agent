import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from threading import Event, Lock
from unittest.mock import patch

from tests.quote_fixture import answer
from work_agent.storage import Store
from work_agent.web import work_pending


class DoneModel:
    def respond(self, history, tools, instructions, *, max_tool_calls):
        return {"status": "completed", "output": [answer("Finished.")]}


class SchedulerContentionTests(unittest.TestCase):
    def test_worker_recovers_after_another_writer_blocks_a_due_check(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            store = Store(data)
            store.initialize()
            due = datetime(2026, 11, 27, 17, tzinfo=UTC)
            store.create_schedule("Prepare a brief", 4, "09:00", now=due - timedelta(days=1))
            stop, wake = Event(), Event()
            with closing(sqlite3.connect(data / "agent.sqlite3")) as writer:
                writer.execute("BEGIN IMMEDIATE")

                def wait(timeout):
                    if writer.in_transaction:
                        writer.rollback()
                    else:
                        stop.set()

                # Use a real competing writer without waiting out SQLite's 5s timeout.
                quick_connect = partial(sqlite3.connect, timeout=0)
                with patch.object(sqlite3, "connect", quick_connect), patch.object(wake, "wait", wait):
                    with self.assertLogs("work_agent.web", level="WARNING"):
                        work_pending(store, DoneModel(), wake, stop, Lock(), lambda: due)

            task, = store.list_tasks()
            self.assertEqual(task["status"], "completed")
            self.assertEqual(store.get_schedule()["latest_run"]["task_id"], task["id"])
