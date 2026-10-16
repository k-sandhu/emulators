"""mrkdwn -> rich_text conversion and block validation (pure functions)."""

from __future__ import annotations

import pytest

from emulators.providers.slack.richtext import (
    inline_elements,
    text_to_blocks,
    validate_attachments,
    validate_blocks,
)
from emulators.providers.slack.runtime import SlackError


def section(text: str) -> list[dict]:
    (block,) = text_to_blocks(text, "seed")
    (first,) = block["elements"]
    assert first["type"] == "rich_text_section"
    return first["elements"]


def test_plain_text_is_one_text_element() -> None:
    assert section("hello world") == [{"type": "text", "text": "hello world"}]
    assert text_to_blocks("", "s") == [] and text_to_blocks("   ", "s") == []


def test_styles_and_nesting() -> None:
    assert section("a *bold* b _it_ c ~gone~ d `code`") == [
        {"type": "text", "text": "a "},
        {"type": "text", "text": "bold", "style": {"bold": True}},
        {"type": "text", "text": " b "},
        {"type": "text", "text": "it", "style": {"italic": True}},
        {"type": "text", "text": " c "},
        {"type": "text", "text": "gone", "style": {"strike": True}},
        {"type": "text", "text": " d "},
        {"type": "text", "text": "code", "style": {"code": True}},
    ]
    assert section("*_both_*") == [{"type": "text", "text": "both", "style": {"bold": True, "italic": True}}]
    assert section("~*x* y~") == [
        {"type": "text", "text": "x", "style": {"strike": True, "bold": True}},
        {"type": "text", "text": " y", "style": {"strike": True}},
    ]
    assert section("`*not bold*`") == [{"type": "text", "text": "*not bold*", "style": {"code": True}}]
    assert section("snake_case_name and 2*3*4") == [{"type": "text", "text": "snake_case_name and 2*3*4"}]


def test_entities() -> None:
    elements = section(
        "<@U0123ABCDE> in <#C0123ABCDE|general> <!channel> <!subteam^S0123|@audit> :tada::+1: <mailto:a@b.example>"
    )
    kinds = [e["type"] for e in elements]
    assert kinds == [
        "user",
        "text",
        "channel",
        "text",
        "broadcast",
        "text",
        "usergroup",
        "text",
        "emoji",
        "emoji",
        "text",
        "link",
    ]
    assert elements[0]["user_id"] == "U0123ABCDE" and elements[2]["channel_id"] == "C0123ABCDE"
    assert elements[4]["range"] == "channel" and elements[6]["usergroup_id"] == "S0123"
    assert [e["name"] for e in elements if e["type"] == "emoji"] == ["tada", "+1"]
    assert elements[-1]["url"] == "mailto:a@b.example"


def test_links_with_and_without_labels() -> None:
    elements = inline_elements("<https://x.example/a?b=1&amp;c=2|label> and <https://y.example>")
    assert elements[0] == {"type": "link", "url": "https://x.example/a?b=1&amp;c=2", "text": "label"}
    assert elements[2] == {"type": "link", "url": "https://y.example"}


def test_html_escapes_are_decoded_in_text() -> None:
    assert section("Q&amp;A &lt;draft&gt;") == [{"type": "text", "text": "Q&A <draft>"}]


def test_lists_quotes_and_code_fences() -> None:
    (block,) = text_to_blocks(
        "intro\n• one\n• two\n\n> quoted\n> more\n1. first\n2. second\n```\nline1\nline2\n```\nafter", "s"
    )
    kinds = [e["type"] for e in block["elements"]]
    assert kinds == [
        "rich_text_section",
        "rich_text_list",
        "rich_text_section",
        "rich_text_quote",
        "rich_text_list",
        "rich_text_preformatted",
        "rich_text_section",
    ]
    assert block["elements"][1]["style"] == "bullet" and len(block["elements"][1]["elements"]) == 2
    assert block["elements"][4]["style"] == "ordered"
    assert block["elements"][5]["elements"][0]["text"] == "line1\nline2"
    assert block["elements"][3]["elements"][0]["text"] == "quoted\nmore"


def test_block_ids_are_stable_and_unique() -> None:
    first = text_to_blocks("x", "1700000000.000100")[0]["block_id"]
    assert first == text_to_blocks("x", "1700000000.000100")[0]["block_id"]
    assert first != text_to_blocks("x", "1700000000.000200")[0]["block_id"]
    blocks = validate_blocks([{"type": "divider"}, {"type": "divider", "block_id": "keep"}], "s")
    assert blocks[0]["block_id"] and blocks[1]["block_id"] == "keep" and blocks[0]["block_id"] != "keep"


def test_validation_errors() -> None:
    with pytest.raises(SlackError) as error:
        validate_blocks("nope", "s")
    assert error.value.error == "invalid_blocks_format"
    with pytest.raises(SlackError) as error:
        validate_blocks([{"type": "unknown"}], "s")
    assert error.value.error == "invalid_blocks"
    with pytest.raises(SlackError) as error:
        validate_attachments([1])
    assert error.value.error == "invalid_attachments"
    assert validate_attachments([{"text": "a"}, {"id": 9, "text": "b"}]) == [
        {"id": 1, "text": "a"},
        {"id": 9, "text": "b"},
    ]
