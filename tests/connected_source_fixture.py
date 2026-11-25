"""Serve one read-only purchasing or warehouse data source for the MCP example."""

import argparse
import csv
from itertools import islice
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field


PAGE_SIZE = 80
SOURCE_TOOLS = {
    "purchasing": ("list_orders", "Read purchase-order lines, with identifiers preserved as text."),
    "warehouse": ("list_receipts", "Read individual delivery receipts, with identifiers preserved as text."),
}


class SourcePage(BaseModel):
    records: list[dict[str, str | int]]
    next_cursor: int | None


def source_records(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source)
        header = next(reader)
        while True:
            start_line = reader.line_num + 1
            cells = next(reader, None)
            if cells is None:
                break
            if cells:
                yield {
                    **dict(zip(header, cells, strict=True)),
                    "source_row": start_line,
                    "source_ref": f"{path.name}:{start_line}",
                }


def create_server(role: str, source_file: Path) -> MCPServer:
    tool_name, description = SOURCE_TOOLS[role]
    server = MCPServer(role)

    @server.tool(
        name=tool_name,
        description=description + " Start with cursor 0 and follow next_cursor until null.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    )
    def list_page(cursor: Annotated[int, Field(ge=0)] = 0) -> SourcePage:
        page = list(islice(source_records(source_file), cursor, cursor + PAGE_SIZE + 1))
        return SourcePage(
            records=page[:PAGE_SIZE],
            next_cursor=cursor + PAGE_SIZE if len(page) > PAGE_SIZE else None,
        )

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=SOURCE_TOOLS, required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not args.file.is_file():
        parser.error(f"Source file does not exist: {args.file}")
    server = create_server(args.role, args.file)
    server.run(
        transport="streamable-http",
        host=args.host,
        port=args.port,
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[f"{args.role}:*", "127.0.0.1:*", "localhost:*"],
        ),
    )


if __name__ == "__main__":
    main()
