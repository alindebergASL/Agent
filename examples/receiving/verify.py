"""Independently reconcile the example inputs and check an exported exceptions CSV."""

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path


HERE = Path(__file__).resolve().parent


def read_rows(path):
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise ValueError(f"Missing CSV header in {path}")
        previous_line = reader.line_num
        for row in reader:
            yield previous_line + 1, row
            previous_line = reader.line_num


def order_key(row):
    return row["po_number"], row["po_line"]


def expected_exceptions(orders, receipts):
    deliveries = defaultdict(list)
    for source_line, row in read_rows(receipts):
        deliveries[order_key(row)].append((source_line, int(row["received_qty"])))

    exceptions = {}
    for source_line, row in read_rows(orders):
        key = order_key(row)
        ordered = int(row["ordered_qty"])
        matching_receipts = deliveries[key]
        received = sum(units for _, units in matching_receipts)
        if received < ordered:
            exceptions[key] = (
                ordered, received, ordered - received, source_line,
                {line for line, _ in matching_receipts},
            )
    return exceptions


def verify_export(path, expected, receipt_filename):
    actual = {}
    for _, row in read_rows(path):
        key = order_key(row)
        if key in actual:
            raise ValueError(f"Duplicate exception {key}")
        po_refs = re.findall(r"purchase-orders\.csv:(\d+)", row["po_source_row"])
        if len(po_refs) != 1:
            raise ValueError(f"Expected one purchase-order source row for {key}")
        receipt_refs = re.findall(rf"{re.escape(receipt_filename)}:(\d+)", row["receipt_source_rows"])
        actual[key] = (
            int(row["ordered_qty"]), int(row["received_qty"]), int(row["short_qty"]),
            int(po_refs[0]), set(map(int, receipt_refs)),
        )
    if actual != expected:
        mismatches = {
            key: {"expected": expected.get(key), "actual": actual.get(key)}
            for key in sorted(expected.keys() | actual.keys())
            if expected.get(key) != actual.get(key)
        }
        raise ValueError(f"Exceptions differ: {mismatches}")


def mutate_receipt(destination):
    with (HERE / "receipts.csv").open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        fields = reader.fieldnames
        rows = list(reader)
    [receipt] = [row for row in rows if row["receipt_id"] == "RC-86263"]
    receipt["received_qty"] = str(int(receipt["received_qty"]) + 9)
    with destination.open("x", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", type=Path, default=HERE / "receipts.csv")
    parser.add_argument("--make-changed-receipts", type=Path)
    parser.add_argument("--exceptions", type=Path)
    args = parser.parse_args()
    if args.make_changed_receipts:
        mutate_receipt(args.make_changed_receipts)
        return

    expected = expected_exceptions(HERE / "purchase-orders.csv", args.receipts)
    if args.exceptions:
        verify_export(args.exceptions, expected, args.receipts.name)
    for (po, line), (ordered, received, shortage, po_row, receipt_rows) in expected.items():
        print(po, line, ordered, received, shortage, po_row, sorted(receipt_rows))
    print(f"{len(expected)} short lines, {sum(row[2] for row in expected.values())} units short")


if __name__ == "__main__":
    main()
