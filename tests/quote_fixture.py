"""Offline Responses test double for the documented browser demonstration."""

import argparse
import csv
import io
import json
from dataclasses import dataclass
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import time

SUFFIX = (
    "Give me a CSV and a short recommendation. Show where each price and freight term "
    "came from, and tell me whether changing the number of shipments could change the choice."
)
REQUESTS = {
    f"Compare the cost of buying ten units delivered in two shipments. {SUFFIX}": 2,
    f"Compare the cost of buying ten units delivered in one shipment. {SUFFIX}": 1,
}
UNITS = 10
# Keep the offline fixture self-contained in the Compose image.
QUOTE_HEADER = b"supplier,quote_id,part_number,unit_price_usd,freight_usd,freight_basis\n"
QUOTE_UPLOADS = {
    "supplier-a.csv": QUOTE_HEADER + b"Supplier A,A-01,00427,100.00,30.00,per shipment\n",
    "supplier-b.csv": QUOTE_HEADER + b"Supplier B,B-01,00427,103.00,20.00,per order\n",
}


def call(call_id, tool_name, **arguments):
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "status": "completed",
        "call_id": call_id,
        "name": tool_name,
        "arguments": json.dumps(arguments),
    }


def answer(message):
    return {
        "type": "message",
        "id": "msg_fixture",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": message, "annotations": []}],
    }


def tool_outputs(items):
    return {
        item["call_id"]: json.loads(item["output"])
        for item in items
        if item["type"] == "function_call_output"
    }


@dataclass(frozen=True)
class Quote:
    source: str
    supplier: str
    quote_id: str
    part_number: str
    unit_price: Decimal
    freight: Decimal
    freight_basis: str

    @classmethod
    def read(cls, result):
        lines = result["lines"]
        text = "\n".join(line["text"] for line in lines)
        rows = list(csv.DictReader(io.StringIO(text)))
        if len(rows) != 1 or len(lines) != 2:
            raise ValueError("Each demonstration quote must have a header and one data row.")
        row, = rows
        return cls(
            f'{result["file_id"]}:{lines[1]["number"]}',
            row["supplier"],
            row["quote_id"],
            row["part_number"],
            Decimal(row["unit_price_usd"]),
            Decimal(row["freight_usd"]),
            row["freight_basis"],
        )

    def multiplier(self, shipments):
        return {"per shipment": shipments, "per order": 1}[self.freight_basis]


def total_call_id(shipments, index):
    return f"total_{shipments}_{index}"


def write_results(primary, quotes, results):
    comparison = io.StringIO(newline="")
    writer = csv.writer(comparison, lineterminator="\n")
    writer.writerow([
        "shipments", "supplier", "quote_id", "part_number", "units", "unit_price_usd",
        "freight_usd", "freight_basis", "freight_charged_usd", "total_usd", "source",
    ])
    recommendations = {}
    for shipments in (primary, 3 - primary):
        totals = {}
        for index, quote in enumerate(quotes):
            total = Decimal(results[total_call_id(shipments, index)]["result"])
            freight = quote.freight * quote.multiplier(shipments)
            writer.writerow([
                shipments, quote.supplier, quote.quote_id, quote.part_number, UNITS,
                f"{quote.unit_price:.2f}", f"{quote.freight:.2f}", quote.freight_basis,
                f"{freight:.2f}", f"{total:.2f}", quote.source,
            ])
            totals[quote.supplier] = total
        ordered = sorted(totals, key=totals.get)
        best, other = ordered
        shipment_phrase = "one shipment" if shipments == 1 else "two shipments"
        detail = " and ".join(f"{supplier} costs ${amount:,.2f}" for supplier, amount in totals.items())
        if totals[best] == totals[other]:
            conclusion = "Both quotes tie."
        else:
            difference = totals[other] - totals[best]
            conclusion = f"Choose {best}, which is ${difference:,.2f} cheaper."
        recommendations[shipments] = f"For {shipment_phrase}, {detail}. {conclusion}"

    sources = "\n".join(
        f"- {q.supplier} ({q.quote_id}, part `{q.part_number}`) quotes "
        f"${q.unit_price:,.2f} per unit and ${q.freight:,.2f} {q.freight_basis} "
        f"in `{q.source}`."
        for q in quotes
    )
    markdown = (
        f"# Recommendation\n\n{recommendations[primary]}\n\n"
        f"{recommendations[3 - primary]}\n\n## Sources\n\n{sources}\n\n"
        "The totals apply the quoted unit prices to all ten units and include quoted "
        "freight only. Taxes, surcharges, or shipment minimums could affect the comparison.\n"
    )
    return [
        call("write_csv", "write_artifact", name="comparison.csv", content=comparison.getvalue()),
        call("write_markdown", "write_artifact", name="recommendation.md", content=markdown),
    ]


class QuoteModel:
    """Compute a scripted response from the uploaded quote rows and arithmetic tool results."""

    def respond(self, input, tools, instructions):
        user = next(item for item in input if item.get("role") == "user")
        task = json.loads(user["content"])
        primary = REQUESTS[task["request"]]
        filenames = task["uploaded_files"]
        if len(filenames) != 2:
            raise ValueError("Upload the two demonstration quotes.")
        results = tool_outputs(input)
        if "write_csv" in results and "write_markdown" in results:
            if results["write_csv"].get("staged") != "comparison.csv":
                raise ValueError("The CSV was not staged.")
            if results["write_markdown"].get("staged") != "recommendation.md":
                raise ValueError("The recommendation was not staged.")
            output = [answer("Saved the comparison and recommendation with quote sources.")]
        elif total_call_id(1, 0) in results:
            quotes = [Quote.read(results[f"read_{index}"]) for index in range(2)]
            output = write_results(primary, quotes, results)
        elif "read_0" in results:
            quotes = [Quote.read(results[f"read_{index}"]) for index in range(2)]
            output = [
                call(
                    total_call_id(shipments, index),
                    "calculate",
                    expression=f"{UNITS} * {quote.unit_price} + {quote.multiplier(shipments)} * {quote.freight}",
                )
                for shipments in (primary, 3 - primary)
                for index, quote in enumerate(quotes)
            ]
        else:
            output = [
                call(f"read_{index}", "read_file", file_id=name, start_line=1, line_count=10)
                for index, name in enumerate(filenames)
            ]
        return {"status": "completed", "output": output}


class Handler(BaseHTTPRequestHandler):
    model = QuoteModel()

    def do_POST(self):
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                raise ValueError("Invalid request size.")
            request = json.loads(self.rfile.read(length))
            response = self.model.respond(request["input"], request["tools"], request["instructions"])
        except (KeyError, StopIteration, TypeError, ValueError) as error:
            self.send_error(400, str(error).encode("latin-1", "replace").decode("latin-1"))
            return
        data = json.dumps({
            "object": "response",
            "id": "resp_quote_fixture",
            "created_at": int(time()),
            "model": request["model"],
            **response,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with ThreadingHTTPServer((args.host, args.port), Handler) as server:
        print(f"Quote fixture listening on port {server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
