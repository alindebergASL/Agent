import tempfile
import unittest
from pathlib import Path

from work_agent.storage import Store


class PassageSearchTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.store.initialize()
        self.consumer = self.store.submit("Look for supporting evidence", {})

    def test_quoted_multiline_record_exact_identifier_and_physical_lines(self):
        data = b'part,record,rate\r\n731,ordinary,3\r\n000731,"special\r\nfreight clause",17\r\n'
        source = self.store.submit("Ingest originals", {"annex.csv": data})
        result, = self.store.search_saved_work(self.consumer, "000731")
        self.assertEqual(result["source_task_id"], source)
        self.assertEqual(result["passage"], {
            "start_line": 3, "end_line": 4, "context": "part,record,rate",
            "text": '000731,"special\nfreight clause",17', "match": "keyword",
        })
        result, = self.store.search_saved_work(self.consumer, "731")
        self.assertEqual(result["passage"]["start_line"], 2)
        guarded, = self.store.search_saved_work(self.consumer, "000731", "upload", ["731"])
        self.assertEqual(guarded["passage"]["start_line"], 3)
        self.assertEqual(self.store.search_saved_work(source, "000731"), [])

    def test_heading_and_late_constraint_visible_and_historical_citation_immutable(self):
        self.store.claim()
        self.store.complete(self.consumer, "Ready")
        source = self.store.submit("Produce notes", {})
        self.assertEqual(self.store.claim(), source)
        body = "# Purchasing\n\n" + "Ordinary terms.\n" * 180 + "\n## Exceptions\n\nFreight applies per shipment."
        self.store.stage(source, "terms.md", body)
        self.store.complete(source, "Saved")
        match, = self.store.search_saved_work(self.consumer, "freight shipment")
        passage = match["passage"]
        self.assertEqual((passage["start_line"], passage["end_line"]), (186, 186))
        self.assertEqual(passage["context"], "Purchasing / Exceptions")
        pinned = self.store.pin_saved_work(self.consumer, source, "terms.md", 1)
        self.store.save_markdown(source, "terms.md", "Transportation is included.", 1)
        self.assertEqual(self.store.search_saved_work(self.consumer, "freight"), [])
        self.assertEqual(self.store.search_saved_work(self.consumer, "transportation")[0]["revision"], 2)
        self.store.initialize()
        self.assertEqual(self.store.read_prior_artifact(self.consumer, pinned["reference_id"]), body.encode())

    def test_distributional_and_model_paraphrase_channels(self):
        self.store.submit("First filing", {"one.txt": b"Shipping vendors assess delivery charges on every consignment."})
        self.store.submit("Second filing", {"two.txt": b"Freight vendors assess delivery charges on every consignment."})
        results = self.store.search_saved_work(self.consumer, "shipping")
        self.assertEqual([(r["name"], r["passage"]["match"]) for r in results],
                         [("one.txt", "keyword"), ("two.txt", "related context")])
        hits = self.store.search_saved_work(self.consumer, "transportation expense", "upload", ["freight"])
        self.assertEqual([(r["name"], r["passage"]["match"]) for r in hits], [("two.txt", "paraphrase")])

    def test_existing_db_backfills_legacy_upload_from_disk(self):
        source = self.store.submit("Earlier assignment", {"old.txt": b"obsolete"})
        with self.store._connection() as db:
            db.execute("UPDATE uploads SET content=NULL WHERE task_id=?", (source,))
            for trigger in ("dirty_passages_upload", "dirty_passages_artifact"):
                db.execute(f"DROP TRIGGER {trigger}")
            for table in ("source_passages", "file_passage_rows", "passage_files"):
                db.execute(f"DROP TABLE {table}")
        path = self.store.root / "runs" / source / "inputs"
        path.mkdir(exist_ok=True, parents=True)
        (path / "0").write_bytes(b"First line\n\nLegacy evidence is here.")
        self.store.initialize()
        self.store.initialize()
        hit, = self.store.search_saved_work(self.consumer, "legacy")
        self.assertEqual(hit["passage"]["start_line"], 3)

    def test_large_file_does_not_crowd_out_another_file(self):
        self.store.submit("Import quotes", {
            "large.csv": b"item,policy\n" + b"part,minimum\n" * 300,
            "small.csv": b"item,policy\nother,minimum",
        })
        hits = self.store.search_saved_work(self.consumer, "minimum", "upload")
        self.assertEqual({r["name"] for r in hits}, {"large.csv", "small.csv"})

    def test_tool_delivers_matched_range_then_requires_pin_and_read(self):
        from unittest.mock import Mock

        from tests.quote_fixture import answer, call, tool_outputs
        from tests.test_prior_runner import read, response
        from work_agent.runner import run_assignment

        self.store.submit("Import schedule", {"prices.csv": b"item,term\nwidget,freight included"})
        model = Mock()
        model.respond.side_effect = [
            response(call("find", "search_saved_work", query="shipping costs", kind="upload", paraphrases=["freight"])),
            response(call("open", "open_saved_work", candidate_id=1)),
            response(read("read", reference_id=1, start_line=2, line_count=1)),
            response(answer("Freight is included.")),
        ]
        trace = []
        run_assignment(
            model, "Find delivery costs", {}, Mock(), Mock(), trace.append,
            search_saved_work=lambda *args: self.store.search_saved_work(self.consumer, *args),
            pin_saved_work=lambda *args: self.store.pin_saved_work(self.consumer, *args),
            read_prior_artifact=lambda ref: self.store.read_prior_artifact(self.consumer, ref),
        )
        outputs = tool_outputs(trace)
        candidate, = outputs["find"]["candidates"]
        self.assertEqual((candidate["kind"], candidate["passage"]["start_line"]), ("upload", 2))
        self.assertEqual(outputs["read"]["lines"], [{"number": 2, "text": "widget,freight included"}])
