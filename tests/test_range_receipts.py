import unittest

from work_agent.calculation_evidence import calculate_with_evidence, range_values
from work_agent.cited_evidence import merge_lines, remember_lines


class RangeReceiptsTests(unittest.TestCase):
    def test_numeric_tokens_require_visible_sign_digits_and_exponent(self):
        for token, part, expected in [
            ("123", "23", "23"),
            ("−25", "25", "25"),
            ("-$25", "$25", "25"),
            ("(USD 25.00)", "USD 25.00", "-25.00"),
            ("3e+2", "3", "3"),
            ("tag42", "42", "42"),
            ("1,234.00", "234.00", "234.00"),
        ]:
            line = f"Earlier {expected}; actual: {token}. End."
            start = line.rindex(token) + token.index(part)
            self.assertNotIn(expected, range_values("record.txt", line, start, start + len(part)))
        line = "Prefix (USD 25.00) suffix"
        self.assertEqual(range_values("record.txt", line, 7, 18), ["-25.00"])
        self.assertEqual(range_values("record.txt", line, 8, 17), [])

    def test_csv_cell_boundaries_and_office_value_column(self):
        line = 'Notes,"memo ""updated""; $1,234",20,30'
        start = line.index("1,234")
        self.assertEqual(range_values("order.csv", line, start, start + 5), ["1234"])
        self.assertEqual(range_values("order.csv", line, start + 2, start + 5), [])
        self.assertEqual(range_values("order.csv", line, line.index("20"), len(line)), ["20", "30"])
        large = '"' + 'q' * 150000 + ' 17"'
        self.assertEqual(range_values("order.csv", large, 149990, len(large)), ["17"])
        formula = "'Rates'!B22\tformula (unevaluated)\t=10*2"
        self.assertEqual(range_values("cost.xlsx", formula, 0, len(formula)), [])
        number = "'Rates'!B22\tnumber\t9.25"
        self.assertEqual(range_values("cost.xlsx", number, 0, len(number)), ["9.25"])

    def test_repeated_reads_keep_separate_excerpt_evidence(self):
        url = "../uploads/example.txt#L1"
        text = "one=12; gap=999; two=30"
        previous = {}
        for start, end in [(0, 7), (17, len(text))]:
            next_batch = {}
            remember_lines(next_batch, "read_line_range", {
                "file_id": "example.txt", "evidence_url": url, "start_char": start, "end_char": end,
                "text": text[start:end], "numeric_values": range_values("example.txt", text, start, end),
            })
            merge_lines(previous, next_batch)

        def calculate(value):
            binding = {"name": "q", "value": value, "source_url": url, "interpretation": "count"}
            return calculate_with_evidence("q", [binding], previous, lambda _, v: v["q"])

        self.assertEqual(calculate("12")["inputs"][0]["source_text"], "one=12;")
        self.assertEqual(calculate("30")["inputs"][0]["start_char"], 17)
        with self.assertRaisesRegex(ValueError, "complete numeric"):
            calculate("999")
