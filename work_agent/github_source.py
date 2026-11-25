"""Serve read-only status and evidence from one operator-selected public GitHub repository."""

import argparse
import json
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, NotRequired, TypedDict

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field


DEFAULT_REPOSITORY = "alindebergASL/Agent"
PAGE_SIZE = 10
NOTE_BYTES = 3500
Positive = Annotated[int, Field(ge=1)]
PullState = Literal["open", "closed", "all"]
NoteKind = Literal["comments", "reviews", "merge_events"]


class Observation(TypedDict):
    repository: str
    observed_at: str
    error: NotRequired[str]


class Page(Observation):
    records: NotRequired[list[dict[str, Any]]]
    next_page: NotRequired[int | None]


class Detail(Observation):
    record: NotRequired[dict[str, Any]]


def _timestamp(value):
    """Compare timezone-aware GitHub timestamps, including equal boundary seconds."""
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or "T" not in value:
        raise ValueError("Use an ISO timestamp with a timezone, such as 2026-11-24T18:00:00Z.")
    return stamp


def _body(record, offset, limit):
    text = record.get("body") or ""
    # Offset counts characters. Budget serialized bytes, including JSON escapes.
    excerpt = text[offset:offset + limit]
    while len(json.dumps(excerpt, ensure_ascii=False).encode()) - 2 > limit:
        excerpt = excerpt[:len(excerpt) // 2]
    end = offset + len(excerpt)
    return {"body": excerpt, "body_truncated": end < len(text), "next_body_offset": end if end < len(text) else None}


class GitHubRepository:
    def __init__(self, repository=DEFAULT_REPOSITORY, *, transport=None):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository) or repository.split("/")[1] in {".", ".."}:
            raise ValueError("Use a GitHub owner/repository name.")
        self.repository = repository
        self.web_url = f"https://github.com/{repository}"
        self.api_url = f"https://api.github.com/repos/{repository}/"
        self.transport = transport

    def _read(self, path, **params):
        observed = {"repository": self.repository, "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            with httpx.Client(transport=self.transport, timeout=6.0, follow_redirects=False) as client:
                response = client.get(self.api_url + path, params=params, headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                    "User-Agent": "Work-Agent-GitHub-Source",
                })
            response.raise_for_status()
            return observed, response.json(), "next" in response.links
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            headers = exc.response.headers
            if status == 429 or (status == 403 and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers)):
                message = "GitHub rate limit reached. Retry later."
            elif status == 404:
                message = "GitHub could not find this public repository or record."
            else:
                message = f"GitHub returned HTTP {status}. This read is incomplete."
        except (httpx.RequestError, ValueError):
            message = "GitHub could not return a readable response. This read is incomplete."
        return {**observed, "error": message}, None, False

    def _pull(self, row):
        number = row["number"]
        merged = bool(row.get("merged_at") or row.get("merged"))
        return {
            "number": number, "title": row["title"], "url": f"{self.web_url}/pull/{number}",
            "state": "merged" if merged else row["state"], "draft": row.get("draft", False),
            "base": row["base"]["ref"], "head_sha": row["head"]["sha"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], "merged_at": row.get("merged_at"),
            "merge_commit_sha": row.get("merge_commit_sha") if merged else None,
        }

    def list_pull_requests(self, state: PullState = "all", updated_since: str | None = None, page: Positive = 1) -> Page:
        """List PRs by most recent update. Each record has number, title, url, state (open/closed/merged),
        draft, base, head_sha, merge_commit_sha, merged_at, and created_at/updated_at. Request open for
        the backlog or all for changes. updated_since includes the specified second and requires a timezone.
        Follow next_page until null. An error means the requested page is missing.
        """
        cutoff = _timestamp(updated_since) if updated_since is not None else None
        observed, rows, more = self._read("pulls", state=state, sort="updated", direction="desc", page=page, per_page=PAGE_SIZE)
        if "error" in observed:
            return observed
        records = []
        for row in rows:
            if cutoff is not None and _timestamp(row["updated_at"]) < cutoff:
                more = False
                break
            records.append(self._pull(row))
        return {**observed, "records": records, "next_page": page + 1 if more else None}

    def get_pull_request(self, number: Positive, body_offset: Annotated[int, Field(ge=0)] = 0) -> Detail:
        """Read a PR's body, head_sha, merge_commit_sha, merged_at, requested_reviewers and requested_teams.
        For a merged PR without merge_commit_sha, read list_pull_request_notes(kind="merge_events") for
        GitHub’s recorded commit_id. Use next_body_offset for more text. A merge establishes repository state only.
        """
        observed, row, _ = self._read(f"pulls/{number}")
        if "error" in observed:
            return observed
        reviewers = [person["login"] for person in row.get("requested_reviewers", [])]
        teams = [team["slug"] for team in row.get("requested_teams", [])]
        record = {**self._pull(row), **_body(row, body_offset, 10000), "requested_reviewers": reviewers, "requested_teams": teams}
        return {**observed, "record": record}

    def list_pull_request_notes(self, number: Positive, kind: NoteKind = "comments", page: Positive = 1,
                                body_offset: Annotated[int, Field(ge=0)] = 0) -> Page:
        """Read comments, formal reviews, or merge events on one PR. Read comments for live verification
        claims and compare the tested commit against head_sha. Reviews carry state and commit_id.
        Merge events carry the commit_id GitHub recorded on merge. Follow next_page, even when a
        timeline page contains no merges. For a clipped body use next_body_offset on the same page.
        """
        paths = {"comments": f"issues/{number}/comments", "reviews": f"pulls/{number}/reviews",
                 "merge_events": f"issues/{number}/timeline"}
        path = paths[kind]
        observed, rows, more = self._read(path, page=page, per_page=PAGE_SIZE)
        if "error" in observed:
            return observed
        records = []
        for row in rows:
            if kind == "merge_events":
                if row.get("event") == "merged":
                    sha = row.get("commit_id")
                    records.append({
                        "id": row["id"], "event": "merged", "author": (row.get("actor") or {}).get("login"),
                        "commit_id": sha, "created_at": row["created_at"],
                        "url": f"{self.web_url}/commit/{sha}" if sha else f"{self.web_url}/pull/{number}",
                    })
                continue
            note_id = row["id"]
            record = {"id": note_id, "author": (row.get("user") or {}).get("login"), **_body(row, body_offset, NOTE_BYTES)}
            if kind == "comments":
                record.update(created_at=row["created_at"], updated_at=row["updated_at"],
                              url=f"{self.web_url}/pull/{number}#issuecomment-{note_id}")
            else:
                record.update(state=row["state"], commit_id=row.get("commit_id"), submitted_at=row.get("submitted_at"),
                              url=f"{self.web_url}/pull/{number}#pullrequestreview-{note_id}")
            records.append(record)
        return {**observed, "records": records, "next_page": page + 1 if more else None}


def create_server(repository=DEFAULT_REPOSITORY, *, transport=None):
    reader = GitHubRepository(repository, transport=transport)
    server = MCPServer("github")
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
    for tool in (reader.list_pull_requests, reader.get_pull_request, reader.list_pull_request_notes):
        server.tool(annotations=annotations)(tool)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    create_server(args.repository).run(
        transport="streamable-http", host=args.host, port=args.port, json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=["github:*", "127.0.0.1:*", "localhost:*"]),
    )


if __name__ == "__main__":
    main()
