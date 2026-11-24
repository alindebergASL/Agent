"""Deterministic recovery inputs and model doubles for the quote demonstration."""

import csv
import io
import json
from dataclasses import dataclass
from decimal import Decimal
from threading import Event

from tests.quote_fixture import answer, call, tool_outputs


ORIGINAL_QUOTES = {
    "north-mill.csv": (
        b"supplier,quote_id,part_number,unit_price_usd,freight_usd,freight_basis\n"
        b"North Mill,NM-1,000731,41.50,8.40,per shipment\n"
        b"North Mill,NM-2,731,57.25,11.30,per order\n"
    ),
}
LOSSY_UPLOAD_NAME = "north-mill-import-attempt.csv"
FRESH_QUOTES = {
    "alder-works.csv": (
        b"part;vendor;quote;unit_usd;freight_usd;charge\n"
        b"000582;Alder Works;AW-1;24.70;7.20;per shipment\n"
        b"582;Alder Works;AW-2;18.40;11.50;per shipment\n"
    ),
    "beacon-metals.csv": (
        b"quote|vendor|charge|freight_usd|part|unit_usd\n"
        b"BM-1|Beacon Metals|per order|13.50|000582|25.40\n"
        b"BM-2|Beacon Metals|per order|9.10|582|17.90\n"
    ),
}
RECOVERY_REQUEST = (
    "Compare buying four units of each item delivered in two shipments on one order. "
    "The uploads include the original quote and a prior import attempt. Check whether the import preserved "
    "the part identifiers and verify the merchandise and freight totals from the source. Save a corrected "
    "CSV and a short Markdown recommendation with source-row citations. If the repair is verified, save "
    "a scoped reusable method."
)
FRESH_REQUEST = (
    "Compare six units of each part across the two supplier quotes, delivered in two shipments on one order. "
    "Save a CSV with the part identifiers, prices, freight, and delivered totals, and a short Markdown "
    "recommendation with source-row citations."
)
CORRECTION_REQUEST = "Discard that comparison and confirm the correction was received."
MARKER_REQUEST = "Confirm that the worker is ready."
CORRECTED_ARTIFACT = "corrected-comparison.csv"
METHOD_TITLE = "Preserve textual part identifiers in quote imports"
METHOD_SCOPE = "Delimited supplier quotes with text identifiers and shipment- or order-based freight."
METHOD_PROCEDURE = (
    "Read the original part identifiers as text before calculating. Compare any import to the originals "
    "by quote row, recover exact identifiers from the originals, and convert only numeric price and freight "
    "fields. Apply per-shipment or per-order freight, verify the totals, and keep source-row citations."
)


def lossy_import(quote: bytes) -> bytes:
    """Simulate a failed importer that inferred integer part identifiers."""
    rows = csv.DictReader(io.StringIO(quote.decode("utf-8")))
    result = io.StringIO(newline="")
    writer = csv.DictWriter(result, fieldnames=rows.fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        row["part_number"] = str(int(row["part_number"]))
        writer.writerow(row)
    return result.getvalue().encode("utf-8")


@dataclass(frozen=True)
class QuoteRow:
    supplier: str
    quote_id: str
    part_number: str
    unit_price: Decimal
    freight: Decimal
    freight_basis: str
    source: str

    def charged_freight(self) -> Decimal:
        return self.freight * {"per shipment": 2, "per order": 1}[self.freight_basis]


def quote_rows(receipt):
    lines = receipt["lines"]
    text = "\n".join(line["text"] for line in lines)
    dialect = csv.Sniffer().sniff(text, delimiters=",;|")
    return [
        QuoteRow(
            row.get("supplier", row.get("vendor")),
            row.get("quote_id", row.get("quote")),
            row.get("part_number", row.get("part")),
            Decimal(row.get("unit_price_usd", row.get("unit_usd"))),
            Decimal(row["freight_usd"]),
            row.get("freight_basis", row.get("charge")),
            f"{receipt['file_id']}:{line['number']}",
        )
        for row, line in zip(csv.DictReader(io.StringIO(text), dialect=dialect), lines[1:], strict=True)
    ]


def response(*output):
    return {"status": "completed", "output": list(output)}


def current_request(history):
    return json.loads(next(item["content"] for item in reversed(history) if item.get("role") == "user"))


def file_reads(filenames):
    return [call(f"read-{name}", "read_file", file_id=name, start_line=1, line_count=10) for name in filenames]


def calculations(quotes, units):
    return [
        call(f"total-{quote.quote_id}", "calculate", expression=f"{units} * {quote.unit_price} + {quote.charged_freight()}")
        for quote in quotes
    ]


def totals_from(receipts, quotes):
    return {quote.quote_id: receipts[f"total-{quote.quote_id}"]["result"] for quote in quotes}


def comparison_csv(quotes, units, totals):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([
        "supplier", "quote_id", "part_number", "units", "unit_price_usd", "freight_basis",
        "freight_charged_usd", "total_usd", "source",
    ])
    for quote in quotes:
        writer.writerow([
            quote.supplier, quote.quote_id, quote.part_number, units, f"{quote.unit_price:.2f}",
            quote.freight_basis, f"{quote.charged_freight():.2f}", totals[quote.quote_id], quote.source,
        ])
    return output.getvalue()


class RecoveryModel:
    """Repair the bad import from source and stage a reusable method after the receipts."""

    def __init__(self, *, hold_final=False):
        self.hold_final = hold_final
        self.waiting = Event()
        self.release = Event()
        self.final_returned = Event()
        self.correction_index = None

    def respond(self, history, tools, instructions, *, max_tool_calls):
        request = current_request(history)
        if request["request"] == MARKER_REQUEST:
            return response(answer("Worker ready."))
        if request["request"] == CORRECTION_REQUEST:
            self.correction_index = request.get("saved_methods", [])
            return response(answer("Correction received."))
        if request["request"] != RECOVERY_REQUEST:
            raise ValueError("Unknown recovery fixture request.")

        receipts = tool_outputs(history)
        if "save-method" in receipts:
            if self.hold_final:
                self.waiting.set()
                if not self.release.wait(timeout=10):
                    raise RuntimeError("Test did not release the delayed response.")
            self.final_returned.set()
            return response(answer("Saved the corrected comparison and the verified method."))

        source, = ORIGINAL_QUOTES
        if f"read-{source}" not in receipts:
            return response(*file_reads([source, LOSSY_UPLOAD_NAME]))
        quotes = quote_rows(receipts[f"read-{source}"])
        imported = {quote.quote_id: quote.part_number for quote in quote_rows(receipts[f"read-{LOSSY_UPLOAD_NAME}"])}
        lost = [quote for quote in quotes if imported[quote.quote_id] != quote.part_number]
        changed, = lost
        if len(set(imported.values())) != 1 or len({quote.part_number for quote in quotes}) != 2:
            raise ValueError("The import did not demonstrate a part identifier collision.")

        if f"total-{quotes[0].quote_id}" not in receipts:
            return response(*calculations(quotes, 4))
        totals = totals_from(receipts, quotes)
        diagnosis = (
            f"The import converted `{changed.part_number}` to `{imported[changed.quote_id]}`, creating a collision. "
            "Recovered the exact part identifiers from the source before comparing prices."
        )
        checks = "\n".join(
            f"- Part `{quote.part_number}` totals ${totals[quote.quote_id]} ({quote.source})." for quote in quotes
        )
        method = {
            "title": METHOD_TITLE,
            "scope": METHOD_SCOPE,
            "procedure": METHOD_PROCEDURE,
            "evidence": f"{diagnosis}\n\n{checks}",
            "source_files": [source, LOSSY_UPLOAD_NAME],
            "checked_results": list(totals.values()),
            "corrected_artifact": CORRECTED_ARTIFACT,
        }
        if "write-csv" not in receipts:
            return response(
                call("write-csv", "write_artifact", name=CORRECTED_ARTIFACT, content=comparison_csv(quotes, 4, totals)),
                call("write-md", "write_artifact", name="recommendation.md", content=f"{diagnosis}\n\n{checks}\n"),
                call("premature-method", "stage_recovery_method", **method),
            )
        changed_totals = {**method, "checked_results": [f"${total}" for total in totals.values()]}
        return response(
            call("unchecked-method", "stage_recovery_method", **changed_totals),
            call("save-method", "stage_recovery_method", **method),
        )


class FreshQuoteModel:
    def __init__(self):
        self.requests = []

    def respond(self, history, tools, instructions, *, max_tool_calls):
        self.requests.append(json.dumps(history))
        request = current_request(history)
        receipts = tool_outputs(history)
        if "write-csv" in receipts:
            return response(answer("Saved a comparison of the new quotes."))
        if "method" not in receipts:
            method, = request["saved_methods"]
            return response(call("method", "read_method", method_id=method["id"]))
        if receipts["method"]["procedure"] != METHOD_PROCEDURE:
            raise ValueError("The expected reusable procedure was not retrieved.")
        if f"read-{request['uploaded_files'][0]}" not in receipts:
            return response(*file_reads(request["uploaded_files"]))
        quotes = [quote for name in request["uploaded_files"] for quote in quote_rows(receipts[f"read-{name}"])]
        if f"total-{quotes[0].quote_id}" not in receipts:
            return response(*calculations(quotes, 6))
        totals = totals_from(receipts, quotes)
        recommendations = []
        for part in dict.fromkeys(quote.part_number for quote in quotes):
            ranked = sorted((quote for quote in quotes if quote.part_number == part), key=lambda quote: Decimal(totals[quote.quote_id]))
            best, other = ranked
            difference = Decimal(totals[other.quote_id]) - Decimal(totals[best.quote_id])
            recommendations.append(
                f"- Part `{part}`: choose {best.supplier} by ${difference:.2f} "
                f"({best.source}, {other.source})."
            )
        return response(
            call("write-csv", "write_artifact", name="comparison.csv", content=comparison_csv(quotes, 6, totals)),
            call("write-md", "write_artifact", name="recommendation.md", content="\n".join(recommendations) + "\n"),
        )
