"""mrkdwn -> ``rich_text`` block conversion, and validation of client-supplied blocks.

Slack stores every message as blocks. Messages posted with only ``text`` come back with an
auto-generated ``rich_text`` block; this module reproduces that closely enough for clients
that read ``blocks`` (mentions, links, emoji, styles, lists, quotes and code fences).
"""

from __future__ import annotations

import re
from typing import Any

from emulators.core.ids import stable_hash
from emulators.providers.slack.runtime import SlackError
from emulators.providers.slack.util import unescape_text

_ENTITY = re.compile(
    r"<@(?P<user>[UW][A-Z0-9]+)(?:\|[^>]*)?>"
    r"|<#(?P<channel>[CGD][A-Z0-9]+)(?:\|[^>]*)?>"
    r"|<!subteam\^(?P<group>[A-Z0-9]+)(?:\|[^>]*)?>"
    r"|<!(?P<range>here|channel|everyone)(?:\|[^>]*)?>"
    r"|<(?P<url>(?:https?|mailto|tel):[^>|]+)(?:\|(?P<label>[^>]*))?>"
    r"|:(?P<emoji>[a-z0-9_+\-]+(?:::skin-tone-[2-6])?):"
)
_STYLES = (
    ("code", re.compile(r"(?<![\w`])`([^`\n]+)`(?![\w`])")),
    ("bold", re.compile(r"(?<![\w*])\*([^\s*](?:[^*\n]*[^\s*])?)\*(?![\w*])")),
    ("italic", re.compile(r"(?<![\w_])_([^\s_](?:[^_\n]*[^\s_])?)_(?![\w_])")),
    ("strike", re.compile(r"(?<![\w~])~([^\s~](?:[^~\n]*[^\s~])?)~(?![\w~])")),
)
_BULLET = re.compile(r"^(?:[•◦▪\-*]\s+)(.*)$")
_NUMBERED = re.compile(r"^\d+[.)]\s+(.*)$")

BLOCK_TYPES = {
    "actions",
    "context",
    "context_actions",
    "divider",
    "file",
    "header",
    "image",
    "input",
    "markdown",
    "rich_text",
    "section",
    "table",
    "video",
}
MAX_BLOCKS = 50


def _styled(text: str, style: dict[str, bool] | None = None) -> list[dict[str, Any]]:
    """Split plain text into ``text`` elements, applying mrkdwn styles (nested ones merge)."""
    if not text:
        return []
    best: tuple[str, re.Match[str]] | None = None
    for name, pattern in _STYLES:
        match = pattern.search(text)
        if match and (
            best is None or (match.start(), -len(match.group(0))) < (best[1].start(), -len(best[1].group(0)))
        ):
            best = (name, match)
    if best is None:
        element: dict[str, Any] = {"type": "text", "text": unescape_text(text)}
        if style:
            element["style"] = dict(style)
        return [element]
    name, match = best
    merged = {**(style or {}), name: True}
    inner = (
        _styled(match.group(1), merged)
        if name != "code"
        else [{"type": "text", "text": unescape_text(match.group(1)), "style": merged}]
    )
    return _styled(text[: match.start()], style) + inner + _styled(text[match.end() :], style)


def inline_elements(text: str) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    cursor = 0
    for match in _ENTITY.finditer(text):
        elements += _styled(text[cursor : match.start()])
        cursor = match.end()
        if match.group("user"):
            elements.append({"type": "user", "user_id": match.group("user")})
        elif match.group("channel"):
            elements.append({"type": "channel", "channel_id": match.group("channel")})
        elif match.group("group"):
            elements.append({"type": "usergroup", "usergroup_id": match.group("group")})
        elif match.group("range"):
            elements.append({"type": "broadcast", "range": match.group("range")})
        elif match.group("url"):
            link: dict[str, Any] = {"type": "link", "url": match.group("url")}
            if match.group("label"):
                link["text"] = match.group("label")
            elements.append(link)
        else:
            elements.append({"type": "emoji", "name": match.group("emoji")})
    elements += _styled(text[cursor:])
    return elements


def _block_id(seed: str, index: int) -> str:
    return stable_hash("block", seed, index, length=5)


def text_to_blocks(text: str, seed: str) -> list[dict[str, Any]]:
    """The ``rich_text`` block Slack generates for a text-only message."""
    if not text.strip():
        return []
    elements: list[dict[str, Any]] = []
    plain: list[str] = []
    lines = text.split("\n")

    def flush_plain() -> None:
        if plain:
            joined = "\n".join(plain)
            elements.append({"type": "rich_text_section", "elements": inline_elements(joined)})
            plain.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            flush_plain()
            body: list[str] = [line[3:]]
            if "```" in line[3:]:
                body = [line[3:].split("```")[0]]
            else:
                index += 1
                while index < len(lines) and "```" not in lines[index]:
                    body.append(lines[index])
                    index += 1
                if index < len(lines):
                    body.append(lines[index].split("```")[0])
            code = "\n".join(body).strip("\n")
            elements.append(
                {
                    "type": "rich_text_preformatted",
                    "elements": [{"type": "text", "text": unescape_text(code)}],
                }
            )
        elif line.startswith("&gt;") or line.startswith(">"):
            flush_plain()
            quoted = [line.removeprefix("&gt;").removeprefix(">").lstrip()]
            while index + 1 < len(lines) and lines[index + 1].startswith((">", "&gt;")):
                index += 1
                quoted.append(lines[index].removeprefix("&gt;").removeprefix(">").lstrip())
            elements.append({"type": "rich_text_quote", "elements": inline_elements("\n".join(quoted))})
        elif _BULLET.match(line) or _NUMBERED.match(line):
            flush_plain()
            ordered = bool(_NUMBERED.match(line))
            items = []
            matcher = _NUMBERED if ordered else _BULLET
            while index < len(lines) and (found := matcher.match(lines[index])):
                items.append({"type": "rich_text_section", "elements": inline_elements(found.group(1))})
                index += 1
            index -= 1
            elements.append(
                {
                    "type": "rich_text_list",
                    "style": "ordered" if ordered else "bullet",
                    "indent": 0,
                    "elements": items,
                }
            )
        else:
            plain.append(line)
        index += 1
    flush_plain()
    return [{"type": "rich_text", "block_id": _block_id(seed, 0), "elements": elements}]


def validate_blocks(blocks: Any, seed: str) -> list[dict[str, Any]]:
    """Check client blocks the way Slack does at a glance and assign missing ``block_id``s."""
    if not isinstance(blocks, list):
        raise SlackError("invalid_blocks_format")
    if len(blocks) > MAX_BLOCKS:
        raise SlackError("invalid_blocks", response_metadata={"messages": ["[ERROR] more than 50 items"]})
    result = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise SlackError(
                "invalid_blocks",
                response_metadata={
                    "messages": [f"[ERROR] must provide a valid type (json-pointer: /blocks/{index})"]
                },
            )
        if block["type"] not in BLOCK_TYPES:
            raise SlackError(
                "invalid_blocks",
                response_metadata={"messages": [f"[ERROR] unsupported type: {block['type']}"]},
            )
        result.append({**block, "block_id": block.get("block_id") or _block_id(seed, index)})
    return result


def validate_attachments(attachments: Any) -> list[dict[str, Any]]:
    if not isinstance(attachments, list) or any(not isinstance(a, dict) for a in attachments):
        raise SlackError("invalid_attachments")
    if len(attachments) > 100:
        raise SlackError("too_many_attachments")
    return [{**a, "id": a.get("id") or position} for position, a in enumerate(attachments, start=1)]
