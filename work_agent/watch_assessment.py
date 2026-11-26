"""Evaluate a watch using this check's observations and its durable comparison point."""

import json
import re
from urllib.parse import quote, urlsplit, urlunsplit

from .cited_evidence import line_source_urls


INSTRUCTIONS = (
    " This is a background watch check. The watch request defines the scope and when attention is useful. "
    "Compare current evidence with the attached subject, the previous successful check and known findings. "
    "The original baseline and past failures are historical. Do not treat a saved claim, a previous receipt, "
    "or a newly generated summary as a fresh source read. For public sources open the relevant actual pages; "
    "for connected sources read the live tool and finish necessary pagination. Visit every required_urls entry. "
    "Honor the user's included and excluded sources when interpreting the standing request. A successful request may still "
    "describe a draft, stale information, an incomplete result or an upstream error. Evaluate its content. "
    "When a workspace item is attached, read its current version with observe_watch_subject on every check. "
    "Read the full item for a baseline or unchanged result unless this exact revision was verified previously. "
    "Its revision can establish a change to that item; verify external claims against the requested sources. "
    "If the user asked to maintain a shared note, read the current revision and stage any justified change "
    "with the operative evidence, preserving earlier decisions and unaffected claims. "
    "Use check_date_pacific as the observation date for deadlines and freshness. Reopen prior evidence "
    "when explaining earlier conclusions. Establish a baseline on the first successful check unless a newly discovered item already warrants "
    "attention. On later passes assess material changes, distinguishing authority, applicability and effective "
    "date. If the relevant event has already been reported, report unchanged, even if a mirror repeats it. "
    "For each new material finding give one stable key for the underlying event or change, independent of "
    "URL, check date or headline, and explain its practical implication. Use different keys for a "
    "subsequent distinct change. When a required source is unavailable or incomplete choose unavailable; "
    "describe what could not be verified. Keep private instructions and memories out of public queries. "
    "After the necessary reads and any requested deliverables, call stage_watch_assessment directly and "
    "alone to complete the check. Put the comparison point for the next check in summary. Cite the exact "
    "public URL of a completed page visit or the evidence_url of a current connected read. Include prior "
    "evidence when explaining what changed. Each material finding must include its own fresh citation."
)

URLS = {"type": "array", "items": {"type": "string"}}
FINDING = {
    "key": {"type": "string", "description": "Stable identity of this specific event or change."},
    "title": {"type": "string"},
    "why": {"type": "string", "description": "Why this matters for this watch and what follows."},
    "evidence": URLS,
}
PARAMETERS = {
    "outcome": {"type": "string", "enum": ["baseline", "material", "unchanged", "unavailable"]},
    "summary": {"type": "string", "description": "What you checked, the state now and what changed, or the coverage gap."},
    "evidence": URLS,
    "findings": {"type": "array", "items": {
        "type": "object", "properties": FINDING, "required": list(FINDING), "additionalProperties": False,
    }},
}


def public_url(value):
    if not isinstance(value, str) or len(value) > 4096 or re.search(r"[\s\\\x00-\x1f]", value):
        return None
    try:
        u = urlsplit(value)
        if u.scheme not in ("https", "http") or not u.hostname or u.username is not None or u.password is not None:
            return None
        # Accessing port validates the provider/user-supplied authority.
        port = u.port
        host = u.hostname.lower()
        if ":" in host:
            host = "[" + host + "]"
        if port and (u.scheme, port) not in {("https", 443), ("http", 80)}:
            host += f":{port}"
        return urlunsplit((u.scheme, host, u.path or "/", u.query, ""))
    except ValueError:
        return None


def request_key(tool, args):
    return tool, json.dumps(args, sort_keys=True, separators=(",", ":"))


class WatchAssessment:
    def __init__(self, context, task_id):
        self.context = context
        self.task_id = task_id
        self.aliases = {}
        self.fresh = set()
        self.incomplete = set()
        self.completed_requests = set()
        self.workspace_pages = {}
        self.visited = set()
        self.required_urls = {public_url(url) for url in context.get("required_urls", [])}
        prior_records = [context.get("previous"), context.get("baseline"), *context.get("known_events", [])]
        for record in prior_records:
            if record:
                for url in record.get("evidence", []):
                    self.aliases[url] = url

    def observe_web(self, item):
        if item.get("type") != "web_search_call":
            return
        action = item.get("action") or {}
        if action.get("type") not in ("open_page", "find_in_page"):
            return
        url = public_url(action.get("url"))
        if url is None:
            return
        if item.get("status") == "completed":
            self.incomplete.discard(("web", url))
            self.aliases[action["url"]] = url
            self.aliases[url] = url
            self.fresh.add(url)
            self.visited.add(url)
        else:
            self.incomplete.add(("web", url))

    def observe(self, tool, result, args, connected=False):
        if tool == "search_watch_findings":
            for finding in result.get("findings", []):
                for url in finding["evidence"]:
                    self.aliases[url] = url
        if connected:
            signature = request_key(tool, args)
            data = result.get("data")
            alias = result.get("evidence_url")
            # An upstream failure may itself have a durable receipt. It can explain
            # an unavailable check, but only a successful read establishes fresh facts.
            if alias:
                url = f"/tasks/{self.task_id}/" + alias.removeprefix("../")
                self.aliases[alias] = self.aliases[url] = url
            if "error" in result or data is None or isinstance(data, dict) and data.get("error"):
                self.incomplete.add(signature)
                self.completed_requests.discard(signature)
                return
            self.incomplete.discard(signature)
            self.completed_requests.add(signature)
            if isinstance(data, dict):
                for field, parameter in (("next_cursor", "cursor"), ("next_page", "page")):
                    nxt = data.get(field)
                    if nxt is not None:
                        next_read = request_key(tool, {**args, parameter: nxt})
                        if next_read not in self.completed_requests:
                            self.incomplete.add(next_read)
            if alias:
                self.fresh.add(url)
            return
        if tool == "observe_watch_subject" and result.get("observed_workspace_subject"):
            base = result["source_url"]
            self.aliases[base] = base
            self.fresh.add(base)
            coverage, _ = self.workspace_pages.setdefault(base, (set(), result["total_lines"]))
            for line in result.get("lines", []):
                coverage.add(line["number"])
                link = line.get("claim_url") or f"{base}#L{line['number']}"
                self.aliases[link] = link
                self.fresh.add(link)
        if tool in ("read_project_note", "read_project_note_revision") and result.get("id"):
            url = f"/notes/{result['id']}?revision={result['revision']}"
            self.aliases[url] = url
        if tool == "read_cited_evidence" and "data" in result:
            self.aliases[result["source_url"]] = result["source_url"]
        if result.get("lines") and tool in ("read_file", "read_saved_artifact", "read_prior_artifact", "read_cited_evidence"):
            owner = result.get("source_task_id", self.task_id)
            rev = result.get("revision")
            kind = "uploads" if rev is None else "artifacts"
            base = f"/tasks/{owner}/{kind}/{quote(result['file_id'], safe='')}" + (f"?revision={rev}" if rev else "")
            for line in result["lines"]:
                suffix = f"#L{line['number']}"
                target = base + suffix
                self.aliases[target] = target
                for prefix in line_source_urls(tool, result):
                    self.aliases[prefix + suffix] = target

    def evidence_urls(self, urls):
        if not isinstance(urls, list) or len(urls) > 24 or not all(isinstance(u, str) for u in urls):
            raise ValueError("Supply up to 24 exact evidence URLs.")
        resolved = []
        for url in urls:
            target = self.aliases.get(url) or self.aliases.get(public_url(url))
            if not target:
                raise ValueError("Cite a URL from an observed source or the watch's verified history.")
            if target not in resolved:
                resolved.append(target)
        return resolved

    def validate(self, args):
        if not isinstance(args, dict) or args.keys() != PARAMETERS.keys():
            raise ValueError("Supply outcome, summary, evidence and findings.")
        outcome, summary, findings = args["outcome"], args["summary"], args["findings"]
        if outcome not in PARAMETERS["outcome"]["enum"] or not isinstance(summary, str) or not summary.strip():
            raise ValueError("Choose a watch outcome and explain the result.")
        if not isinstance(findings, list) or bool(findings) != (outcome == "material"):
            raise ValueError("Include actionable findings only for material results.")
        evidence = self.evidence_urls(args["evidence"])
        if outcome != "unavailable":
            if self.context.get("subject") and not self.workspace_pages:
                raise ValueError("Read the attached current workspace subject before assessing this watch, or report unavailable.")
            if self.incomplete:
                raise ValueError("A checked source failed or has unread pages. Finish the read or report unavailable.")
            if self.required_urls - self.visited:
                raise ValueError("Visit every public URL specified in the watch, or explain the gap as unavailable.")
            if not self.fresh.intersection(evidence):
                raise ValueError("A successful assessment needs a fresh source observation in this check.")
            if outcome in ("unchanged", "baseline"):
                previous = self.context.get("previous") or {}
                prior_bases = {url.partition("#")[0] for url in previous.get("evidence", [])}
                for base, (covered, total) in self.workspace_pages.items():
                    same_version = previous.get("outcome") in ("baseline", "unchanged") and base in prior_bases
                    if len(covered) != total and not same_version:
                        raise ValueError("Finish reading this workspace revision before reporting its baseline or no change.")
            if outcome == "baseline" and self.context.get("previous"):
                raise ValueError("This watch has a successful prior assessment. Compare the current evidence with it.")
            if outcome == "unchanged" and not self.context.get("previous"):
                raise ValueError("Establish a baseline on the first successful check or report a new material finding.")
        checked = []
        for item in findings:
            if not isinstance(item, dict) or item.keys() != FINDING.keys() or not all(
                isinstance(item[field], str) and item[field].strip() for field in ("key", "title", "why")
            ):
                raise ValueError("Give each finding a stable key, title, implication and sources.")
            citations = self.evidence_urls(item["evidence"])
            if not set(citations).issubset(evidence) or not self.fresh.intersection(citations):
                raise ValueError("Each finding needs fresh evidence, also included in the check's evidence.")
            checked.append({**item, "evidence": citations})
        return {"outcome": outcome, "summary": summary.strip(), "evidence": evidence, "findings": checked}
