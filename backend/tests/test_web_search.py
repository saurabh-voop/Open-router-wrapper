from app.agent.tools.web_search import citations_from_annotations


def test_citations_are_numbered_deduplicated_and_web_only() -> None:
    def ann(url: object) -> dict[str, object]:
        return {"type": "url_citation", "url_citation": {"url": url, "title": "T"}}

    cites = citations_from_annotations(
        [
            ann("https://a.example/x"),
            ann("https://a.example/x"),
            ann("javascript:alert(1)"),
            ann("data:text/html,hi"),
            ann(None),
            {"type": "file", "file": {}},
            ann("http://b.example"),
        ]
    )
    assert cites == [
        {"index": 1, "url": "https://a.example/x", "title": "T"},
        {"index": 2, "url": "http://b.example", "title": "T"},
    ]
