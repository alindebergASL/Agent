import json
import unittest

from work_agent.research import web_search_observations


class ResearchTests(unittest.TestCase):
    def test_history_preserves_native_search_open_and_citation(self):
        url = "https://example.edu/resources/handbook"
        search = {
            "type": "web_search_call",
            "id": "ws_search",
            "status": "completed",
            "action": {
                "type": "search",
                "queries": ["example university handbook"],
                "sources": [{"type": "url", "url": url}],
            },
        }
        opened = {
            "type": "web_search_call",
            "id": "ws_open",
            "status": "completed",
            "action": {"type": "open_page", "url": url},
        }
        found = {
            "type": "web_search_call",
            "id": "ws_find",
            "status": "completed",
            "action": {"type": "find_in_page", "url": url, "pattern": "handbook"},
        }
        citation = {
            "type": "url_citation",
            "url": url,
            "title": "University Handbook",
            "start_index": 13,
            "end_index": 32,
        }
        output = [
            search,
            opened,
            found,
            {"type": "function_call", "name": "write_artifact"},
            {
                "type": "message",
                "id": "msg_brief",
                "content": [{
                    "type": "output_text",
                    "text": "I saved the University Handbook brief.",
                    "annotations": [citation, {"type": "file_citation"}],
                }],
            },
        ]

        observations = list(web_search_observations(output))
        self.assertEqual(json.loads(json.dumps(observations)), [
            search, opened, found, {**citation, "message_id": "msg_brief"},
        ])

    def test_no_observations_for_plain_response(self):
        output = [{
            "type": "message",
            "content": [{"type": "output_text", "text": "Saved."}],
        }]
        self.assertEqual(list(web_search_observations(output)), [])
