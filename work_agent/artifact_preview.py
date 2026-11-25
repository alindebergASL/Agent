"""Render Markdown with escaped HTML and scoped citation links."""

import re
from html import escape
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML


FILE_CITATION = re.compile(
    r"(?:\.\.|/tasks/(?P<task>[a-f0-9]{32}))/(?P<kind>uploads|artifacts)/"
    r"(?P<name>(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+)"
    r"(?:\?revision=(?P<revision>[1-9][0-9]{0,17}))?(?:#L(?P<line>[1-9][0-9]{0,6}))?"
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
    elif not match["revision"] or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.(csv|md)", name):
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
        if re.fullmatch(r"\.\./prior-artifacts/[1-9][0-9]{0,18}(?:#L[1-9][0-9]{0,6})?", url):
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
    def image(self, tokens, idx, options, env):
        # Keep alt text without contacting an external host when a preview opens.
        return escape(self.renderInlineAsText(tokens[idx].children, options, env))


markdown_preview = ArtifactMarkdown(
    "commonmark", {"html": False}, renderer_cls=ArtifactRenderer,
).enable("table")
