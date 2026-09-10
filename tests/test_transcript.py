"""Tests for transcript HTML at the boundary between model text and a browser.

All content is escaped already; these tests pin the separate rule that only an
explicit URL allowlist may become a clickable anchor.
"""

from __future__ import annotations

import base64
import hashlib
from html.parser import HTMLParser
from typing import Any

import pytest

from pi_agent.host import transcript


@pytest.mark.parametrize(
    "url",
    ["javascript:alert%281%29", "javascript:location='https://evil.example'", "data:text/html,x"],
)
def test_an_unsafe_url_never_becomes_a_clickable_link(url: str) -> None:
    rendered = transcript.markdown(f"[docs]({url})")

    assert "<a " not in rendered
    assert "docs (" in rendered


@pytest.mark.parametrize("url", ["JaVaScRiPt:alert%281%29", "DATA:text/html,x"])
def test_scheme_rejection_ignores_case(url: str) -> None:
    assert "<a " not in transcript.markdown(f"[docs]({url})")


@pytest.mark.parametrize("url", ["%6aavascript:alert%281%29", "&#106;avascript:alert%281%29"])
def test_encoded_scheme_variants_are_inert(url: str) -> None:
    assert "<a " not in transcript.markdown(f"[docs]({url})")


@pytest.mark.parametrize(
    "url", ["http://example.com/x", "https://example.com/x?a=1&b=2", "/docs", "./x", "#part", "a/b"]
)
def test_http_https_and_local_relative_links_still_render(url: str) -> None:
    rendered = transcript.markdown(f"[docs]({url})")

    assert "<a " in rendered
    assert 'rel="noreferrer"' in rendered


@pytest.mark.parametrize("url", ["//evil.example/x", "\\\\evil.example\\share", "file:///tmp/x"])
def test_network_path_and_file_links_are_inert(url: str) -> None:
    assert "<a " not in transcript.markdown(f"[docs]({url})")


def test_link_text_and_destination_are_html_escaped() -> None:
    rendered = transcript.markdown("[<img src=x onerror=alert(1)>](https://ok.example/?a=1&b=2)")

    assert "<img" not in rendered
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered
    assert 'href="https://ok.example/?a=1&amp;b=2"' in rendered


def test_a_malformed_link_is_left_as_literal_text() -> None:
    rendered = transcript.markdown("[x](jav ascript:alert%281%29) and [open](https://x")

    assert "<a " not in rendered
    assert "[x](jav ascript" in rendered


class LinkCollector(HTMLParser):
    """Collect href attributes from the fully rendered page."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.hrefs.extend(
                value for name, value in attrs if name == "href" and value is not None
            )


def test_every_page_anchor_passes_the_same_allowlist() -> None:
    records: list[dict[str, Any]] = [
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "[safe](https://example.com) [bad](javascript:alert%281%29)",
                    }
                ],
            },
        }
    ]
    collector = LinkCollector()

    collector.feed(transcript.render_page(records, "hostile", 1000))

    assert collector.hrefs == ["https://example.com"]
    assert all(transcript._safe_href(href) == href for href in collector.hrefs)


def test_the_page_csp_allows_only_its_exact_inline_script() -> None:
    rendered = transcript.render_page([], "empty", 1000)
    digest = base64.b64encode(hashlib.sha256(transcript.JS.encode()).digest()).decode()

    assert "default-src &#x27;none&#x27;" in rendered
    assert f"sha256-{digest}" in rendered
