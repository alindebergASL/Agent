import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin

from fastapi.testclient import TestClient

from work_agent.storage import Store
from work_agent.web import create_app


class PreviewMarkup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def http_links(self):
        return [
            attrs["href"] for tag, attrs in self.elements
            if tag == "a" and attrs.get("href", "").startswith(("https://", "http://"))
            and not attrs["href"].startswith("http://testserver/")
        ]


class ArtifactPreviewTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(directory)
        self.store.initialize()
        # Seed completed outputs without starting an assignment worker.
        self.client = TestClient(create_app(data_dir=directory))
        self.addCleanup(self.client.close)

    def publish(self, name, content, uploads=None):
        task_id = self.store.submit("Publish an artifact.", uploads or {})
        self.assertEqual(self.store.claim(), task_id)
        self.store.stage(task_id, name, content)
        self.store.complete(task_id, "Saved.")
        return f"/tasks/{task_id}/artifacts/{name}"

    def test_markdown_preview_links_citations_and_keeps_download_bytes(self):
        citation = "https://ai.uiowa.edu/university-supported-ai-tools"
        reference = "http://example.org/handbook?section=ai&year=2026"
        markdown = (
            "# Campus resources — AI\r\n\r\n"
            f"[University-Supported AI Tools]({citation})\r\n\r\n"
            "| Topic | Source |\r\n| --- | --- |\r\n| AI instruction | [Handbook][resource] |\r\n"
            f'\r\n[resource]: {reference} "Staff handbook"\r\n'
        )
        url = self.publish("brief.md", markdown)
        preview = self.client.get(url)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(PreviewMarkup(preview.text).http_links(), [citation, reference])
        self.assertIn("<table>", preview.text)
        self.assertIn("<h1>Campus resources — AI</h1>", preview.text)

        download = self.client.get(url, params={"download": 1})
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, markdown.encode("utf-8"))
        self.assertEqual(download.headers["content-type"], "text/markdown; charset=utf-8")
        self.assertTrue(download.headers["content-disposition"].startswith("attachment;"))

    def test_markdown_preview_blocks_non_web_links_and_escapes_html(self):
        markdown = (
            "<script>alert('raw')</script>\n\n"
            '<a href="https://example.org/raw">Raw markup</a>\n\n'
            '<img src="https://example.org/tracker" onerror="alert(1)">\n\n'
            "[Script](jav&#x61;script:alert(1))\n"
            "[Data](data:text/html,boom)\n"
            "[File](file:///tmp/example)\n"
            "[Mail](mailto:test@example.org)\n"
            "[Relative](/tasks)\n"
            "[Protocol-relative](//example.org/tracker)\n"
            "[Credentials](https://name:password@example.org/)\n"
            "![External **image**](https://example.org/tracker)\n\n"
            "`[Code](https://example.org/code)`\n"
        )
        response = self.client.get(self.publish("brief.md", markdown))
        self.assertEqual(response.status_code, 200)
        markup = PreviewMarkup(response.text)
        self.assertEqual(markup.http_links(), [])
        self.assertEqual(
            sum(tag == "a" for tag, _ in markup.elements), 3,
            "Only the app navigation and download links should be active.",
        )
        self.assertTrue(all(tag not in {"script", "img"} for tag, _ in markup.elements))
        self.assertIn("&lt;script&gt;", response.text)
        self.assertIn("Raw markup&lt;/a&gt;", response.text)
        self.assertIn("External image", response.text)

    def test_upload_row_citations_link_only_to_scoped_sources(self):
        name = "q uôte.csv"
        encoded = quote(name, safe="")
        citation = f"../uploads/{encoded}#L2"
        markdown = (
            f"[Original row]({citation})\n"
            "[Traversal](../uploads/%2e%2e#L2)\n"
            "[Encoded path](../uploads/file%2Fwrong.csv#L2)\n"
            "[Backslash](../uploads/file%5Cwrong.csv#L2)\n"
            "[Relative download](../uploads/source.csv?download=1#L2)\n"
            "[Wrong path](../../uploads/source.csv#L2)\n"
            "[Wrong line](../uploads/source.csv#L0)\n"
        )
        original = b"column\r\n<script>row</script>\r\n"
        url = self.publish("brief.md", markdown, uploads={name: original})
        markup = PreviewMarkup(self.client.get(url).text)
        relative_links = [
            attrs["href"] for tag, attrs in markup.elements
            if tag == "a" and attrs.get("href", "").startswith("../")
        ]
        self.assertEqual(relative_links, [citation])

        source = f"{url.rsplit('/artifacts/', 1)[0]}/uploads/{encoded}"
        preview = self.client.get(source)
        self.assertEqual(preview.status_code, 200)
        self.assertIn('id="L2"', preview.text)
        self.assertIn("&lt;script&gt;row&lt;/script&gt;", preview.text)
        self.assertNotIn("<script>", preview.text)
        self.assertEqual(self.client.get(source, params={"download": 1}).content, original)

    def test_assignment_links_open_uploads_with_literal_url_delimiters(self):
        uploads = {
            "quote#1.csv": b"sku\r\nhash-001\r\n",
            "quôte ?%.csv": b"sku\r\nquestion-002\r\n",
        }
        artifact = self.publish("brief.md", "Saved.", uploads=uploads)
        location = artifact.rsplit("/artifacts/", 1)[0]
        assignment = self.client.get(location)
        self.assertEqual(assignment.status_code, 200)
        links = {
            attrs["href"].rsplit("/", 1)[-1]: attrs["href"]
            for tag, attrs in PreviewMarkup(assignment.text).elements
            if tag == "a" and "/uploads/" in attrs.get("href", "")
        }

        for name, content in uploads.items():
            with self.subTest(name=name):
                href = links[quote(name, safe="")]
                preview = self.client.get(href)
                self.assertEqual(preview.status_code, 200)
                self.assertIn(content.decode().splitlines()[1], preview.text)
                download_href, = (
                    attrs["href"] for tag, attrs in PreviewMarkup(preview.text).elements
                    if tag == "a" and attrs.get("class") == "download"
                )
                download = self.client.get(urljoin(href, download_href))
                self.assertEqual(download.status_code, 200)
                self.assertEqual(download.content, content)

    def test_csv_stays_plain_escaped_text(self):
        csv = "value\n[Text](https://example.org/)\n<script>inert</script>\n"
        url = self.publish("comparison.csv", csv)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        markup = PreviewMarkup(response.text)
        self.assertIn(("pre", {}), markup.elements)
        self.assertEqual(markup.http_links(), [])
        self.assertIn("&lt;script&gt;inert&lt;/script&gt;", response.text)
        self.assertEqual(self.client.get(url, params={"download": 1}).content, csv.encode())
