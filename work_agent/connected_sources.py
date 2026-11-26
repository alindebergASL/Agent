"""Operator-selected, read-only MCP tools for the existing assignment runner."""

import json
import os
import re
from contextlib import ExitStack, asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import httpx2
from anyio.from_thread import start_blocking_portal
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CONNECTION_CLOSED, INVALID_PARAMS, REQUEST_TIMEOUT

READ_TIMEOUT = 10
MAX_WIRE_BYTES = 512 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_SCHEMA_BYTES = 16 * 1024
NAME = re.compile(r"[A-Za-z0-9_-]+")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    tools: tuple[str, ...]
    token_env: str | None


def _source(entry):
    if not isinstance(entry, dict) or set(entry) - {"name", "url", "tools", "token_env"}:
        raise ValueError("Invalid connected source configuration.")
    name, url, tools = (entry.get(field) for field in ("name", "url", "tools"))
    if not isinstance(name, str) or not NAME.fullmatch(name) or "__" in name or name.endswith("_"):
        raise ValueError("Invalid connected source name.")
    if not isinstance(tools, list) or not tools:
        raise ValueError("Each source needs a list of allowed tools.")
    for tool in tools:
        if not isinstance(tool, str) or not NAME.fullmatch(tool) or len(f"mcp_{name}__{tool}") > 64:
            raise ValueError("Invalid connected tool name.")
    if len(set(tools)) != len(tools):
        raise ValueError("Duplicate connected tool name.")
    if not isinstance(url, str):
        raise ValueError("Invalid connected source URL.")
    target = urlsplit(url)
    if (target.scheme not in {"http", "https"} or not target.hostname
            or target.username is not None or target.password is not None or target.query or target.fragment):
        raise ValueError("Use an HTTP(S) endpoint without embedded credentials, a query, or a fragment.")
    token_env = entry.get("token_env")
    if token_env is not None and (target.scheme != "https" or not isinstance(token_env, str) or not ENV_NAME.fullmatch(token_env)):
        raise ValueError("A bearer credential needs HTTPS and an environment-variable name.")
    return Source(name, url, tuple(tools), token_env)


class ConnectedSources:
    def __init__(self, sources):
        self.sources = sources

    @classmethod
    def from_file(cls, path):
        config = json.loads(Path(path).read_bytes())
        if not isinstance(config, dict) or set(config) != {"sources"} or not isinstance(config["sources"], list):
            raise ValueError("Expected an object with a sources list.")
        sources = [_source(entry) for entry in config["sources"]]
        if len({source.name for source in sources}) != len(sources):
            raise ValueError("Connected source names must be unique.")
        if len(sources) > 8 or sum(len(source.tools) for source in sources) > 32:
            raise ValueError("Configure at most 8 connected sources and 32 tools.")
        return cls(sources)

    @contextmanager
    def open(self):
        catalog = SourceCatalog()
        with ExitStack() as stack:
            if self.sources:
                portal = stack.enter_context(start_blocking_portal())
                for source in self.sources:
                    try:
                        remote = portal.wrap_async_context_manager(_open_source(source))
                        client, definitions, token = remote.__enter__()
                        # Closing an unavailable peer cannot override this assignment’s outcome.
                        stack.callback(_close_source, remote)
                    except MissingSourceCredential:
                        catalog.warnings.append(f"Source {source.name} is unavailable because its configured credential is missing.")
                        continue
                    except Exception:
                        catalog.warnings.append(f"Source {source.name} is unavailable.")
                        continue
                    for tool in source.tools:
                        name = f"mcp_{source.name}__{tool}"
                        definition = definitions.get(tool)
                        if definition is None:
                            catalog.warnings.append(f"Connected tool {name} is unavailable.")
                            continue
                        catalog.tools.append(definition)
                        catalog.calls[name] = (portal, client, source.name, tool, token)
            yield catalog


def _close_source(remote):
    with suppress(Exception):
        remote.__exit__(None, None, None)


class _BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        total = 0
        async for chunk in self.stream:
            total += len(chunk)
            if total > MAX_WIRE_BYTES:
                raise ValueError("MCP response exceeds the wire limit.")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


async def _bound_response(response):
    if response.is_redirect:
        raise ValueError("Use the final MCP endpoint URL.")
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise ValueError("MCP response requires identity encoding.")
    # SSE is a stream of individually bounded events; the SDK bounds each one.
    if response.headers.get("content-type", "").split(";")[0].strip().lower() != "text/event-stream":
        response.stream = _BoundedStream(response.stream)


def _json_size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())


def _output_schema(data_schema):
    # Embedding a schema in the envelope changes the root for local references.
    encoded = json.dumps(data_schema, ensure_ascii=False)
    if not data_schema or len(encoded.encode()) > 4096 or re.search(r'"\$(?:ref|dynamicRef|id)"\s*:', encoded):
        data_schema = {}
    return {
        "type": "object",
        "properties": {
            "source": {"type": "string"}, "tool": {"type": "string"},
            "data": data_schema, "evidence_url": {"type": "string"}, "error": {"type": "string"},
            "error_kind": {"type": "string"},
        },
        "anyOf": [{"required": ["source", "tool", "data"]}, {"required": ["error"]}],
    }


class MissingSourceCredential(ValueError):
    pass


@asynccontextmanager
async def _open_source(source):
    token = os.environ.get(source.token_env) if source.token_env else None
    if source.token_env and not token:
        raise MissingSourceCredential()
    headers = {"Accept-Encoding": "identity"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with anyio.fail_after(READ_TIMEOUT) as deadline:
        async with httpx2.AsyncClient(headers=headers, timeout=READ_TIMEOUT, follow_redirects=False, trust_env=False,
                                      event_hooks={"response": [_bound_response]}) as http:
            transport = streamable_http_client(source.url, http_client=http, max_sse_event_size=MAX_WIRE_BYTES)
            async with Client(transport, read_timeout_seconds=READ_TIMEOUT) as client:
                definitions = await _discover(client, source, token)
                # The model may reason for a while between calls. Only network operations have a deadline.
                deadline.deadline = float("inf")
                try:
                    yield client, definitions, token
                finally:
                    deadline.deadline = anyio.current_time() + READ_TIMEOUT


async def _discover(client, source, token):
    definitions = {}
    cursor = None
    for _ in range(16):
        page = await client.list_tools(cursor=cursor)
        for tool in page.tools:
            if tool.name not in source.tools:
                continue
            hints = tool.annotations
            if not hints or hints.read_only_hint is not True or hints.destructive_hint is True:
                continue
            if tool.input_schema.get("type") != "object":
                continue
            definition = {
                "type": "function",
                "name": f"mcp_{source.name}__{tool.name}",
                "description": tool.description or f"Read {tool.name} from {source.name}.",
                "parameters": tool.input_schema,
                "output_schema": _output_schema(tool.output_schema),
                "strict": False,
                "allowed_callers": ["direct", "programmatic"],
            }
            if _json_size(definition) <= MAX_SCHEMA_BYTES and not _contains_token(definition, token):
                definitions[tool.name] = definition
        cursor = page.next_cursor
        if not cursor or len(definitions) == len(source.tools):
            break
    return definitions


def _contains_token(value, token):
    return bool(token and token in json.dumps(value, ensure_ascii=False))


def _read_error(kind, message):
    # Remote diagnostics can contain credentials, filesystem paths or instructions.
    # Publish only local messages and MCP's machine-readable status.
    return {"error_kind": kind, "error": message}


class SourceCatalog:
    def __init__(self):
        self.tools = []
        self.warnings = []
        self.calls = {}

    def call(self, name, arguments):
        if name not in self.calls:
            return _read_error("tool_unavailable", "Choose an available connected tool.")
        if not isinstance(arguments, dict):
            return _read_error("invalid_arguments", "Pass connected tool arguments as a JSON object.")
        portal, client, source, tool, token = self.calls[name]
        try:
            result = portal.call(_read_result, client, tool, arguments)
            if result.is_error:
                return _read_error("source_tool_error", f"{name} reported a tool failure. This read supplied no verified data.")
            data = result.structured_content
            if data is None:
                if any(block.type != "text" for block in result.content):
                    return _read_error("unsupported_content", f"{name} returned unsupported content. Request text or structured data.")
                texts = [block.text for block in result.content]
                data = texts[0] if len(texts) == 1 else texts
            if _contains_token(data, token):
                return _read_error("result_withheld", f"{name} returned a credential and its result was withheld.")
            if _json_size(data) > MAX_RESULT_BYTES:
                return _read_error("result_too_large", f"{name} returned too much data. Request a smaller page or narrower fields.")
        except MCPError as exc:
            if exc.code == INVALID_PARAMS:
                return _read_error("invalid_arguments", f"{name} reported invalid parameters. Check its input schema.")
            if exc.code == REQUEST_TIMEOUT:
                return _read_error("timeout", f"{name} timed out before the read completed.")
            if exc.code == CONNECTION_CLOSED:
                return _read_error("transport_error", f"The connection to {name} closed before the read completed.")
            return _read_error("protocol_error", f"{name} reported an MCP protocol error.")
        except (TimeoutError, httpx2.TimeoutException):
            return _read_error("timeout", f"{name} timed out before the read completed.")
        except httpx2.TransportError:
            return _read_error("transport_error", f"The connection to {name} failed before the read completed.")
        except Exception:
            return _read_error("read_failed", f"{name} could not complete the read.")
        return {"source": source, "tool": tool, "data": data}


async def _read_result(client, tool, arguments):
    with anyio.fail_after(READ_TIMEOUT):
        return await client.session.call_tool(tool, arguments)
