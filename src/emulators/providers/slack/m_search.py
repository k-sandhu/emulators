"""search.messages / search.files / search.all (user tokens only), with Slack's query modifiers."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.slack import access
from emulators.providers.slack.runtime import USER, Actor, Call, SlackError, api
from emulators.providers.slack.shapes import file_json, file_visible, message_json, permalink_for
from emulators.providers.slack.util import decode_cursor, encode_cursor, ts_key, ts_to_float

SKIP_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_archive",
    "channel_unarchive",
    "channel_name",
    "channel_topic",
    "channel_purpose",
    "pinned_item",
    "tombstone",
}
HIGHLIGHT = ("", "")
MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
LINK = re.compile(r"<((?:https?|mailto):[^>|]+)(?:\|([^>]*))?>")


@dataclass
class Query:
    terms: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    from_users: list[str] = field(default_factory=list)
    to_users: list[str] = field(default_factory=list)
    after: float | None = None
    before: float | None = None
    has: set[str] = field(default_factory=set)
    not_channels: list[str] = field(default_factory=list)
    not_from: list[str] = field(default_factory=list)
    not_has: set[str] = field(default_factory=set)
    is_thread: bool = False
    unresolved: bool = False


def _day_start(day: date) -> float:
    return datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()


def _parse_day(ctx: ProviderContext, value: str) -> date | None:
    value = value.strip().lower()
    today = ctx.clock.now().date()
    if value == "today":
        return today
    if value == "yesterday":
        return today - timedelta(days=1)
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    return None


def _resolve_user(ctx: ProviderContext, actor: Actor, token: str) -> str | None:
    token = token.strip().strip("<>").lstrip("@")
    if token.lower() == "me":
        return actor.uid
    if token.startswith(("U", "W")) and access.user_by_id(ctx, token.split("|")[0]):
        return token.split("|")[0]
    wanted = token.lower()
    for user in access.col(ctx, access.USERS):
        names = {
            user["name"].lower(),
            user["profile"].get("display_name", "").lower(),
            user["real_name"].lower(),
        }
        if wanted in names:
            return user["id"]
    return None


def parse_query(ctx: ProviderContext, actor: Actor, text: str) -> Query:
    query = Query()
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        tokens = text.split()
    for token in tokens:
        negated = token.startswith("-") and len(token) > 1
        body = token[1:] if negated else token
        key, sep, value = body.partition(":")
        key = key.lower()
        if (
            sep
            and value
            and key in ("in", "from", "to", "with", "after", "before", "on", "during", "has", "is")
        ):
            if key == "in":
                ref = value.lstrip("#").strip("<>")
                channel = access.channel_by_id(ctx, ref.split("|")[0]) or access.channel_by_name(ctx, ref)
                if channel is None and value.startswith("@"):
                    uid = _resolve_user(ctx, actor, value)
                    channel = access.find_im(ctx, actor.uid, uid) if uid else None
                if channel is None:
                    query.unresolved = query.unresolved or not negated
                else:
                    (query.not_channels if negated else query.channels).append(channel["id"])
            elif key in ("from", "to", "with"):
                uid = _resolve_user(ctx, actor, value)
                if uid is None:
                    query.unresolved = query.unresolved or not negated
                elif key == "from":
                    (query.not_from if negated else query.from_users).append(uid)
                else:
                    query.to_users.append(uid)
            elif key in ("after", "before", "on", "during"):
                _apply_date(ctx, query, key, value)
            elif key == "has":
                (query.not_has if negated else query.has).add(value.lower())
            elif key == "is" and value.lower() == "thread":
                query.is_thread = True
            continue
        (query.excluded if negated else query.terms).append(body.lower())
    return query


def _apply_date(ctx: ProviderContext, query: Query, key: str, value: str) -> None:
    if key == "during":
        match = re.fullmatch(r"(\d{4})(?:-(\d{2}))?", value)
        if match:
            year, month = int(match.group(1)), match.group(2)
            start = date(year, int(month), 1) if month else date(year, 1, 1)
            end = date(year + (int(month or 12) == 12), (int(month) % 12) + 1 if month else 1, 1)
            query.after, query.before = _day_start(start) - 0.000001, _day_start(end)
        return
    day = _parse_day(ctx, value)
    if day is None:
        return
    if key == "after":
        query.after = _day_start(day + timedelta(days=1)) - 0.000001
    elif key == "before":
        query.before = _day_start(day)
    else:
        query.after, query.before = _day_start(day) - 0.000001, _day_start(day + timedelta(days=1))


def searchable_text(ctx: ProviderContext, rec: dict[str, Any]) -> str:
    def mention(match: re.Match[str]) -> str:
        user = access.user_by_id(ctx, match.group(1))
        return "@" + user["name"] if user else match.group(0)

    text = MENTION.sub(mention, rec.get("text", ""))
    text = LINK.sub(lambda m: f"{m.group(2) or ''} {m.group(1)}", text)
    for attachment in rec.get("attachments", []):
        text += " " + " ".join(str(attachment.get(k, "")) for k in ("title", "text", "fallback"))
    return text


def _has(ctx: ProviderContext, flag: str, rec: dict[str, Any], channel: dict[str, Any]) -> bool:
    text = rec.get("text", "")
    if flag == "link":
        return "http" in text or bool(LINK.search(text))
    if flag in ("reaction", "reactions"):
        return bool(rec.get("reactions"))
    if flag == "pin":
        return access.col(ctx, access.PINS).get(access.message_key(channel["id"], rec["ts"])) is not None
    if flag in ("file", "attachment"):
        return bool(rec.get("_files"))
    if flag == "emoji":
        return bool(re.search(r":[a-z0-9_+\-]+:", text))
    return True


def _matches(
    ctx: ProviderContext, query: Query, rec: dict[str, Any], channel: dict[str, Any]
) -> float | None:
    if query.channels and channel["id"] not in query.channels:
        return None
    if channel["id"] in query.not_channels or rec.get("user") in query.not_from:
        return None
    if query.from_users and rec.get("user") not in query.from_users:
        return None
    if query.to_users and not (
        channel.get("is_im")
        and any(u in channel["_members"] and u != rec.get("user") for u in query.to_users)
    ):
        return None
    stamp = ts_to_float(rec["ts"])
    if query.after is not None and stamp <= query.after:
        return None
    if query.before is not None and stamp >= query.before:
        return None
    if query.is_thread and not rec.get("thread_ts"):
        return None
    if any(not _has(ctx, flag, rec, channel) for flag in query.has):
        return None
    if any(_has(ctx, flag, rec, channel) for flag in query.not_has):
        return None
    haystack = searchable_text(ctx, rec).lower()
    if any(term in haystack for term in query.excluded):
        return None
    score = 0.0
    for term in query.terms:
        needle = term.replace("*", "")
        if needle not in haystack:
            return None
        score += haystack.count(needle)
    return score


def _highlight(text: str, terms: list[str]) -> str:
    for term in terms:
        needle = term.replace("*", "")
        if needle:
            text = re.sub(f"({re.escape(needle)})", f"{HIGHLIGHT[0]}\\1{HIGHLIGHT[1]}", text, flags=re.I)
    return text


def _channel_stub(channel: dict[str, Any], actor: Actor) -> dict[str, Any]:
    stub: dict[str, Any] = {
        "id": channel["id"],
        "is_channel": channel.get("is_channel", False),
        "is_group": channel.get("is_group", False),
        "is_im": channel.get("is_im", False),
        "is_mpim": channel.get("is_mpim", False),
        "is_shared": channel.get("is_shared", False),
        "is_org_shared": False,
        "is_ext_shared": channel.get("is_ext_shared", False),
        "is_private": channel.get("is_private", False) or channel.get("is_im", False),
        "pending_shared": [],
        "is_pending_ext_shared": False,
    }
    if channel.get("is_im"):
        stub["user"] = next((m for m in channel["_members"] if m != actor.uid), actor.uid)
        stub["name"] = stub["user"]
    else:
        stub["name"] = channel["name"]
    return stub


def _paginate(call: Call, total: int) -> tuple[int, int, int, int]:
    count = call.params.int("count", 20, lo=1, hi=100) or 20
    cursor = call.params.str("cursor")
    if cursor and cursor != "*":
        value = decode_cursor(cursor, "page")
        if value is None or not value.isdigit():
            raise SlackError("invalid_cursor")
        page = int(value)
    else:
        page = call.params.int("page", 1, lo=1, hi=100) or 1
    pages = max(1, -(-total // count))
    return count, page, pages, total


def _envelope(count: int, page: int, pages: int, total: int) -> dict[str, Any]:
    first = (page - 1) * count + 1 if total else 0
    return {
        "pagination": {
            "total_count": total,
            "page": page,
            "per_page": count,
            "page_count": pages,
            "first": first,
            "last": min(page * count, total),
        },
        "paging": {"count": count, "total": total, "page": page, "pages": pages},
    }


def _search_messages(call: Call, query_text: str) -> dict[str, Any]:
    ctx, actor = call.ctx, call.me
    query = parse_query(ctx, actor, query_text)
    found: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    if not query.unresolved:
        for rec in access.col(ctx, access.MESSAGES):
            if rec.get("subtype") in SKIP_SUBTYPES:
                continue
            channel = access.channel_by_id(ctx, rec["_channel"])
            if channel is None:
                continue
            if channel_kind_hidden(ctx, actor, channel):
                continue
            score = _matches(ctx, query, rec, channel)
            if score is not None:
                found.append((score, rec, channel))
    sort = call.params.str("sort", "score")
    if sort not in ("score", "timestamp"):
        raise SlackError("invalid_sort")
    descending = call.params.str("sort_dir", "desc") != "asc"
    if sort == "score":
        found.sort(key=lambda t: (t[0], ts_key(t[1]["ts"])), reverse=descending)
    else:
        found.sort(key=lambda t: ts_key(t[1]["ts"]), reverse=descending)
    count, page, pages, total = _paginate(call, len(found))
    window = found[(page - 1) * count : page * count]
    highlight = call.params.bool("highlight")
    matches = []
    for score, rec, channel in window:
        shown = message_json(ctx, call.request, actor, rec)
        text = _highlight(shown["text"], query.terms) if highlight else shown["text"]
        match = {
            "iid": "iid-" + rec["id"],
            "team": access.team_id(ctx),
            "score": round(score * 0.01, 6),
            "channel": _channel_stub(channel, actor),
            "type": "message",
            "user": shown.get("user"),
            "username": (access.user_by_id(ctx, shown.get("user")) or {}).get(
                "name", shown.get("username", "")
            ),
            "ts": shown["ts"],
            "blocks": shown.get("blocks", []),
            "text": text,
            "permalink": permalink_for(rec),
        }
        if not rec.get("reactions"):
            match["no_reactions"] = True
        if shown.get("files"):
            match["files"] = shown["files"]
        matches.append(match)
    body = {"total": total, **_envelope(count, page, pages, total), "matches": matches}
    if call.params.str("cursor"):
        body["response_metadata"] = {
            "next_cursor": encode_cursor("page", str(page + 1)) if page < pages else ""
        }
    return body


def channel_kind_hidden(ctx: ProviderContext, actor: Actor, channel: dict[str, Any]) -> bool:
    """Search only covers conversations the caller could read: public ones and their own."""
    if access.is_member(channel, actor.uid):
        return False
    return not (
        access.channel_kind(channel) == "public" and not access.is_guest(access.user_by_id(ctx, actor.uid))
    )


def _search_files(call: Call, query_text: str) -> dict[str, Any]:
    ctx, actor = call.ctx, call.me
    query = parse_query(ctx, actor, query_text)
    found = []
    if not query.unresolved:
        for record in access.col(ctx, access.FILES):
            if not file_visible(ctx, actor, record):
                continue
            if query.from_users and record["user"] not in query.from_users:
                continue
            if query.channels and not any(s["channel"] in query.channels for s in record.get("_shares", [])):
                continue
            stamp = float(record["created"])
            if (query.after is not None and stamp <= query.after) or (
                query.before is not None and stamp >= query.before
            ):
                continue
            haystack = f"{record['name']} {record['title']} {record['filetype']}".lower()
            if any(t in haystack for t in query.excluded) or not all(
                t.replace("*", "") in haystack for t in query.terms
            ):
                continue
            found.append(record)
    order = {r["id"]: position for position, r in enumerate(found)}
    found.sort(
        key=lambda r: (r["created"], order[r["id"]]), reverse=call.params.str("sort_dir", "desc") != "asc"
    )
    count, page, pages, total = _paginate(call, len(found))
    window = found[(page - 1) * count : page * count]
    return {
        "total": total,
        **_envelope(count, page, pages, total),
        "matches": [file_json(ctx, call.request, r) for r in window],
    }


@api("search.messages", scopes=("search:read",), kinds=(USER,))
async def search_messages(call: Call) -> dict[str, Any]:
    query = call.params.str("query")
    if not query:
        raise SlackError("missing_query")
    return {"query": query, "messages": _search_messages(call, query)}


@api("search.files", scopes=("search:read",), kinds=(USER,))
async def search_files(call: Call) -> dict[str, Any]:
    query = call.params.str("query")
    if not query:
        raise SlackError("missing_query")
    return {"query": query, "files": _search_files(call, query)}


@api("search.all", scopes=("search:read",), kinds=(USER,))
async def search_all(call: Call) -> dict[str, Any]:
    query = call.params.str("query")
    if not query:
        raise SlackError("missing_query")
    return {"query": query, "messages": _search_messages(call, query), "files": _search_files(call, query)}
