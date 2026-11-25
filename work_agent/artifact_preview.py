"""Render Markdown with escaped HTML and scoped citation links."""

import re
from html import escape
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML


UPLOAD_CITATION = re.compile(r"\.\./uploads/((?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+)#L[1-9][0-9]{0,5}")


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
        if re.fullmatch(r"\.\./prior-artifacts/[1-9][0-9]{0,18}", url):
            return True
        citation = UPLOAD_CITATION.fullmatch(url)
        if citation:
            try:
                return is_upload_name(unquote(citation[1], errors="strict"))
            except UnicodeDecodeError:
                return False

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
