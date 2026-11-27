import unittest
from unittest.mock import Mock

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.runner import InvalidToolArguments, _line_range, run_assignment


class LineRangeTests(unittest.TestCase):
    def test_unicode_continuation_reconstructs_original_line(self):
        line = "a😀e\u0301" * 4500
        content = "header\r\n" + line + "\r\n"
        offset = 0
        parts = []
        while offset is not None:
            result = _line_range(content, "x.txt", "../uploads/x.txt", None, 2, offset, 3999)
            self.assertEqual(result["evidence_url"], "../uploads/x.txt#L2")
            self.assertEqual(result["start_char"], offset)
            self.assertEqual(result["line_length"], len(line))
            self.assertEqual(result["end_char"] - offset, len(result["text"]))
            self.assertLessEqual(len(result["text"]), 3999)
            parts.append(result["text"])
            offset = result["next_start_char"]
        self.assertEqual("".join(parts), line)
        eof = _line_range(content, "x.txt", "../uploads/x.txt", None, 2, len(line), 1)
        self.assertEqual(eof["text"], "")
        self.assertIsNone(eof["next_start_char"])

    def test_rejects_invalid_line_offset_or_count(self):
        for line, offset, count in (
            (True, 0, 1), (0, 0, 1), (2, 0, 1), (1, -1, 1), (1, True, 1),
            (1, 4, 1), (1, 0, 0), (1, 0, 4001), (1, 0, False),
        ):
            with self.subTest(line=line, offset=offset, count=count), self.assertRaises(InvalidToolArguments):
                _line_range("abc", "x", "../uploads/x", None, line, offset, count)

    def test_pinned_source_current_revision_and_pdf_reads(self):
        batches = [
            [call("prior", "read_line_range", source=17, line_number=1, start_char=0, char_count=4),
             call("unknown", "read_line_range", source=18, line_number=1, start_char=0, char_count=4),
             call("saved", "read_line_range", source={"artifact": "note.md"}, line_number=1, start_char=1, char_count=2),
             call("blank", "read_line_range", source={"upload": "a b.pdf"}, line_number=1, start_char=0, char_count=1),
             {**call("pdf", "read_line_range", source={"upload": "a b.pdf"}, line_number=2, start_char=2, char_count=3),
              "caller": {"type": "program", "caller_id": "batch"}}],
            [call("write", "write_artifact", name="note.md", content="Changed.")],
            [answer("Done.")],
        ]
        model = Mock()
        model.respond.side_effect = [{"status": "completed", "output": batch} for batch in batches]
        records, stage = [], Mock()
        prior_reader = Mock(return_value=b"past")
        run_assignment(
            model, "Check these sources.", {"a b.pdf": "\npage two"}, stage, Mock(), records.append,
            saved_artifacts={"note.md": {"revision": 2}}, read_saved_artifact=lambda _: b"notes",
            prior_artifacts=[{"reference_id": 17, "name": "prior.txt", "source_task_id": "source", "revision": 1}],
            read_prior_artifact=prior_reader, pdf_layouts={"a b.pdf": [
                {"number": 1, "start_line": 1, "end_line": 1, "has_text": False},
                {"number": 2, "start_line": 2, "end_line": 2, "has_text": True},
            ]},
        )
        output = tool_outputs(records)
        prior_reader.assert_called_once_with(17)
        self.assertEqual(output["prior"]["evidence_url"], "../prior-artifacts/17#L1")
        self.assertEqual(output["prior"]["text"], "past")
        self.assertIn("error", output["unknown"])
        self.assertEqual(output["saved"]["evidence_url"], "../artifacts/note.md?revision=2#L1")
        self.assertEqual(output["saved"]["text"], "ot")
        self.assertIn("error", output["blank"])
        self.assertEqual(output["pdf"]["evidence_url"], "../uploads/a%20b.pdf?page=2#L2")
        self.assertEqual(output["pdf"]["text"], "ge ")
        self.assertIn("error", output["write"])
        stage.assert_not_called()
