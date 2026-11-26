"""Render Markdown with escaped HTML and scoped citation links."""

import re
from html import escape
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML


FILE_CITATION = re.compile(
    r"(?:\.\.|/tasks/(?P<task>[a-f0-9]{32}))/(?P<kind>uploads|artifacts)/"
    r"(?P<name>(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+)"
    r"(?:\?revision=(?P<revision>[1-9][0-9]{0,17})|\?page=(?P<page>[1-9][0-9]{0,5}))?"
    r"(?:#L(?P<line>[1-9][0-9]{0,6}))?"
)


PRIOR_CITATION = re.compile(
    r"\.\./prior-artifacts/(?P<reference>[1-9][0-9]{0,18})"
    r"(?:(?:\?page=(?P<page>[1-9][0-9]{0,5}))?#L(?P<line>[1-9][0-9]{0,6}))?"
)


def file_citation(url: str) -> tuple[str | None, str, int | None, int | None] | None:
    match = FILE_CITATION.fullmatch(url)
    if not match:
        return None
    try:
        name = unquote(match["name"], errors="strict")
    except UnicodeDecodeError:
        return None
    if not is_upload_name(name):
        return None
    if match["kind"] == "uploads":
        if match["revision"] or not match["line"]:
            return None
    elif match["page"] or not match["revision"] or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.(csv|md)", name):
        return None
    return match["task"], name, int(match["revision"] or 0) or None, int(match["line"] or 0) or None


def is_upload_name(name: str) -> bool:
    return (
        0 < len(name) <= 120
        and name not in {".", ".."}
        and name.isprintable()
        and "/" not in name
        and "\\" not in name
    )


class ArtifactMarkdown(MarkdownIt):
    def validateLink(self, url: str) -> bool:
        # Allow same-assignment source rows without opening arbitrary relative URLs.
        if re.fullmatch(r"\.\./evidence/[a-f0-9]{32}", url):
            return True
        if PRIOR_CITATION.fullmatch(url):
            return True
        if file_citation(url):
            return True

        try:
            target = urlsplit(url)
        except ValueError:
            return False
        return (
            target.scheme in {"http", "https"}
            and bool(target.hostname)
            and target.username is None
            and super().validateLink(url)
        )


class ArtifactRenderer(RendererHTML):
    def link_open(self, tokens, idx, options, env):
        # Tools supply artifact-relative links. An assignment result is displayed
        # one directory higher than an artifact preview.
        token = tokens[idx]
        href = token.attrGet("href") or ""
        if env.get("assignment_url") and href.startswith("../"):
            token.attrSet("href", env["assignment_url"] + "/" + href[3:])
        return self.renderToken(tokens, idx, options, env)

    def image(self, tokens, idx, options, env):
        # Keep alt text without contacting an external host when a preview opens.
        return escape(self.renderInlineAsText(tokens[idx].children, options, env))


markdown_preview = ArtifactMarkdown(
    "commonmark", {"html": False}, renderer_cls=ArtifactRenderer,
).enable("table")
