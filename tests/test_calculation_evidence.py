import unittest
from decimal import Decimal
from unittest.mock import Mock

from tests.quote_fixture import answer, call, tool_outputs
from work_agent.calculation_evidence import numeric_values
from work_agent.runner import run_assignment


def response(*items):
    return {"status": "completed", "output": list(items)}


class CalculationEvidenceTests(unittest.TestCase):
    def test_signed_currency_cannot_bind_the_opposite_value(self):
        for line in ("Balance −25.00 USD.", "Provision: -$25.", "Credit: (USD 25.00)", "Margin: $ - 25"):
            with self.subTest(line=line):
                self.assertEqual(list(numeric_values(line)), [Decimal("-25")])
        self.assertEqual(list(numeric_values("(25.00)")), [])

    def test_bind_rate_and_separate_governing_passage_only_after_read(self):
        upload = "policy #1.md"
        base = "../uploads/policy%20%231.md#L"
        binding = {"name": "rate", "value": "12.50", "source_url": base + "2",
                   "interpretation": "Service credit per eligible event"}
        condition = {"applies_to": "rate", "source_url": base + "3",
                     "interpretation": "The first remedial event is exempt."}

        def calc(label, expression="(3 - 1) * rate", inputs=None, conditions=None):
            return call(label, "calculate_with_evidence", expression=expression,
                        inputs=[binding] if inputs is None else inputs,
                        conditions=[condition] if conditions is None else conditions)

        model = Mock()
        model.respond.side_effect = [
            response(call("read", "read_file", file_id=upload, start_line=1, line_count=3), calc("premature")),
            response(calc("valid"), calc("unbound", expression="3 * 12.50"),
                     calc("invented", inputs=[{**binding, "value": "2.50"}]),
                     calc("wrongline", conditions=[{**condition, "source_url": base + "6"}])),
            response(answer("Two eligible events produce a $25 credit.")),
        ]
        records = []
        run_assignment(model, "What service credit applies to three events?",
                       {upload: "Service credits\nUSD 12.50.\nExempt the first remedial event."},
                       Mock(), Mock(), records.append)
        out = tool_outputs(records)
        self.assertEqual(out["valid"]["result"], "25.00")
        self.assertEqual(out["valid"]["inputs"][0]["source_text"], "USD 12.50.")
        self.assertEqual(out["valid"]["conditions"][0]["source_text"], "Exempt the first remedial event.")
        for name in ("premature", "unbound", "invented", "wrongline"):
            self.assertIn("error", out[name])

    def test_same_filename_pins_separate_original_and_published_revision(self):
        model = Mock()
        name = "price.csv"
        model.respond.side_effect = [
            response(call("upload", "read_file", file_id=name, start_line=1, line_count=1),
                     call("revision", "read_saved_artifact", name=name, start_line=1, line_count=1)),
            response(call("change", "calculate_with_evidence", expression="new - old", inputs=[
                {"name": "old", "value": "100", "source_url": "../uploads/price.csv#L1",
                 "interpretation": "Original estimate"},
                {"name": "new", "value": "1200", "source_url": "../artifacts/price.csv?revision=2#L1",
                 "interpretation": "Revised estimate"}], conditions=[])),
            response(answer("The estimate increased by 1,100.")),
        ]
        records = []
        run_assignment(model, "Compare revisions.", {name: "100,200"}, Mock(), Mock(), records.append,
                       saved_artifacts={name: {"revision": 2, "author": "user"}},
                       read_saved_artifact=lambda _: b'updated,"1,200"')
        self.assertEqual(tool_outputs(records)["change"]["result"], "1100")
