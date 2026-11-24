"""Render Markdown with tables, escaped HTML, and HTTP(S)-only links."""

from html import escape
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML


class ArtifactMarkdown(MarkdownIt):
    def validateLink(self, url: str) -> bool:
        # Relative links and destinations with credentials stay inert in previews.
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
