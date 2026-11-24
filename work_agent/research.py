from collections.abc import Iterable, Iterator


WEB_SEARCH_TOOL = {"type": "web_search"}


def web_search_observations(output: Iterable[dict]) -> Iterator[dict]:
    """Select the provider's research actions and citations for the run record."""
    for item in output:
        if item.get("type") == "web_search_call":
            yield {
                "type": "web_search_call",
                "id": item.get("id"),
                "status": item.get("status"),
                "action": item.get("action", {}),
            }
        elif item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") != "output_text":
                    continue
                for annotation in part.get("annotations", []):
                    if annotation.get("type") == "url_citation":
                        yield {**annotation, "message_id": item.get("id")}
