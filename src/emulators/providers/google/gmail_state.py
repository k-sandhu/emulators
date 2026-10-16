"""Mailbox state, message delivery and monotonic per-user history."""

from __future__ import annotations

import base64
import copy
import json
from email.utils import format_datetime, parseaddr, parsedate_to_datetime
from typing import Any

from fastapi import Request

from emulators.core.clock import isoformat_z
from emulators.core.ids import b64url, b64url_decode, stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.google.common import google_error, principal
from emulators.providers.google.gmail_mime import addresses, decode_raw, parse_raw, seed_message
from emulators.providers.google.gmail_query import compile_query

SYSTEM_LABELS = (
    "INBOX",
    "SENT",
    "DRAFT",
    "SPAM",
    "TRASH",
    "UNREAD",
    "STARRED",
    "IMPORTANT",
    "CHAT",
    "CATEGORY_PERSONAL",
    "CATEGORY_SOCIAL",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
)


def messages(ctx: ProviderContext, owner: str) -> Any:
    return ctx.store["gmail_messages:" + owner]


def labels(ctx: ProviderContext, owner: str) -> Any:
    return ctx.store["gmail_labels:" + owner]


def mailbox(request: Request, ctx: ProviderContext, user_id: str) -> str:
    actor = principal(request, ctx)
    if actor.app_only or not actor.person_id or not ctx.store["gmail_mailboxes"].get(actor.person_id):
        raise google_error(403, "Precondition check failed.", reason="failedPrecondition")
    person = ctx.world.person(actor.person_id)
    if user_id not in ("me", person.email):
        raise google_error(403, "Delegation denied for " + user_id, reason="forbidden")
    if actor.scopes:
        require_scope(request, actor.scopes)
    return actor.person_id


def require_scope(request: Request, scopes: list[str]) -> None:
    """Empty control-plane scopes stay lenient; explicit grants follow discovery scopes."""
    path = request.url.path
    reading = request.method == "GET"
    allowed = {"gmail.modify"}
    full_mail = True
    if "/settings/" in path:
        if not reading:
            allowed, full_mail = {"gmail.settings.basic"}, False
        else:
            allowed |= {"gmail.readonly", "gmail.settings.basic"}
    elif "/labels" in path:
        allowed.add("gmail.labels")
        if reading:
            allowed |= {"gmail.readonly", "gmail.metadata"}
    elif "/drafts" in path:
        allowed |= {"gmail.compose", "gmail.addons.current.action.compose"}
        if reading:
            allowed.add("gmail.readonly")
    elif request.method == "DELETE" or path.endswith("/batchDelete"):
        allowed = set()
    elif path.endswith("/send"):
        allowed |= {"gmail.send", "gmail.compose", "gmail.addons.current.action.compose"}
    elif not reading and path.endswith(("/import", "/insert", "/messages")):
        allowed.add("gmail.insert")
    elif reading or path.endswith(("/watch", "/stop")):
        allowed |= {"gmail.readonly", "gmail.metadata"}
        if path.endswith("/profile"):
            allowed.add("gmail.compose")
    wanted = {"https://www.googleapis.com/auth/" + value for value in allowed}
    if full_mail:
        wanted.add("https://mail.google.com/")
    if not wanted.intersection(scopes):
        raise google_error(
            403, "Request had insufficient authentication scopes.", reason="insufficientPermissions"
        )
    metadata_only = "https://www.googleapis.com/auth/gmail.metadata" in scopes and not set(
        scopes
    ).intersection(
        {
            "https://mail.google.com/",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
        }
    )
    if metadata_only and (
        request.query_params.get("q")
        or "/attachments/" in path
        or (
            reading
            and any(segment in path for segment in ("/messages/", "/threads/"))
            and request.query_params.get("format", "full").lower() not in ("minimal", "metadata")
        )
    ):
        raise google_error(
            403,
            "Metadata scope does not allow access to message bodies or search.",
            reason="insufficientPermissions",
        )


def seed(ctx: ProviderContext) -> None:
    for person in ctx.world.employees:
        if not person.active:
            continue
        ctx.store["gmail_mailboxes"].put({"id": person.id, "historyId": 1000, "floor": 1000})
        for label in SYSTEM_LABELS:
            labels(ctx, person.id).put({"id": label, "name": label, "type": "system"})
    for thread in ctx.world.email_threads:
        thread_id = stable_hash(ctx.world.seed, "gmail-thread", thread.id)
        for message in thread.messages:
            participants = dict.fromkeys((message.from_id, *message.to_ids, *message.cc_ids))
            for owner in participants:
                if not ctx.store["gmail_mailboxes"].get(owner):
                    continue
                message_labels = ["SENT"] if owner == message.from_id else ["INBOX"]
                if owner in (*message.to_ids, *message.cc_ids) and owner not in message.read_by:
                    message_labels.append("UNREAD")
                if owner in message.flagged_by:
                    message_labels.append("STARRED")
                if message.importance == "high":
                    message_labels.append("IMPORTANT")
                message_labels.append(
                    "CATEGORY_UPDATES"
                    if thread.category in ("vendor", "notification")
                    else "CATEGORY_PERSONAL"
                )
                for name in thread.labels:
                    label = labels(ctx, owner).first(name=name)
                    if not label:
                        number = len([r for r in labels(ctx, owner) if r["type"] == "user"]) + 1
                        label = {
                            "id": f"Label_{number}",
                            "name": name,
                            "type": "user",
                            "messageListVisibility": "show",
                            "labelListVisibility": "labelShow",
                        }
                        labels(ctx, owner).put(label)
                    message_labels.append(label["id"])
                record = seed_message(ctx, message, thread_id, ctx.world.person(owner).email, message_labels)
                messages(ctx, owner).put(record)


def get_message(ctx: ProviderContext, owner: str, message_id: str) -> dict[str, Any]:
    record = messages(ctx, owner).get(message_id)
    if record is None:
        raise google_error(404, "Requested entity was not found.", reason="notFound")
    return record


def reference(record: dict[str, Any], *, with_labels: bool = True) -> dict[str, Any]:
    return {
        "id": record["id"],
        "threadId": record["threadId"],
        **({"labelIds": list(record["labelIds"])} if with_labels else {}),
    }


async def push(ctx: ProviderContext, owner: str, changed_labels: list[str]) -> None:
    watch = ctx.store["gmail_watches"].get(owner)
    if not watch or watch["expires"] <= ctx.clock.timestamp() * 1000:
        return
    wanted = set(watch.get("labelIds", []))
    intersects = bool(wanted.intersection(changed_labels))
    if wanted and (
        (watch.get("labelFilterBehavior", "include") == "include" and not intersects)
        or (watch.get("labelFilterBehavior") == "exclude" and intersects)
    ):
        return
    config = ctx.store["gmail_pubsub"].get(watch["topicName"])
    if not config:
        return
    history_id = str(ctx.store["gmail_mailboxes"].get(owner)["historyId"])
    body = {
        "message": {
            "data": base64.b64encode(
                json.dumps(
                    {"emailAddress": ctx.world.person(owner).email, "historyId": history_id},
                    separators=(",", ":"),
                ).encode()
            ).decode(),
            "messageId": ctx.ids.digits(16),
            "publishTime": isoformat_z(ctx.clock.now()),
        },
        "subscription": watch["topicName"].split("/topics/")[0] + "/subscriptions/emulator-push",
    }
    await ctx.webhooks.deliver(config["endpoint"], json.dumps(body).encode(), event_type="gmail.pubsub")


async def change(
    ctx: ProviderContext, owner: str, record: dict[str, Any], kind: str, label_ids: list[str] | None = None
) -> None:
    box = ctx.store["gmail_mailboxes"].get(owner)
    box["historyId"] += 1
    record["historyId"] = str(box["historyId"])
    ref = reference(record)
    entry = {
        "id": str(box["historyId"]),
        "messages": [ref],
        "_labels": list(dict.fromkeys(record["labelIds"] + (label_ids or []))),
    }
    key = {
        "messageAdded": "messagesAdded",
        "messageDeleted": "messagesDeleted",
        "labelAdded": "labelsAdded",
        "labelRemoved": "labelsRemoved",
    }[kind]
    entry[key] = [{"message": ref, **({"labelIds": label_ids} if label_ids is not None else {})}]
    ctx.store["gmail_history:" + owner].put(entry)
    await push(ctx, owner, entry["_labels"])


def validate_labels(ctx: ProviderContext, owner: str, ids: list[str]) -> None:
    if not isinstance(ids, list) or any(not labels(ctx, owner).get(label) for label in ids):
        raise google_error(400, "Invalid label", reason="invalidArgument")


async def modify(ctx: ProviderContext, owner: str, record: dict[str, Any], body: dict[str, Any]) -> None:
    add, remove = body.get("addLabelIds", []), body.get("removeLabelIds", [])
    validate_labels(ctx, owner, add)
    validate_labels(ctx, owner, remove)
    added = [label for label in add if label not in record["labelIds"]]
    removed = [label for label in remove if label in record["labelIds"]]
    record["labelIds"] = list(
        dict.fromkeys([label for label in record["labelIds"] if label not in remove] + add)
    )
    if added:
        await change(ctx, owner, record, "labelAdded", added)
    if removed:
        await change(ctx, owner, record, "labelRemoved", removed)


async def remove_message(ctx: ProviderContext, owner: str, record: dict[str, Any]) -> None:
    messages(ctx, owner).delete(record["id"])
    for draft in ctx.store["gmail_drafts:" + owner].where(messageId=record["id"]):
        ctx.store["gmail_drafts:" + owner].delete(draft["id"])
    await change(ctx, owner, record, "messageDeleted")


async def ingest(
    request: Request, ctx: ProviderContext, owner: str, body: dict[str, Any], mode: str
) -> dict[str, Any]:
    data = decode_raw(body)
    message_id, thread_id = ctx.ids.hex(16), ctx.ids.hex(16)
    record = parse_raw(
        ctx, data, message_id, thread_id, ["SENT"] if mode == "send" else body.get("labelIds", [])
    )
    validate_labels(ctx, owner, record["labelIds"])
    if mode == "send":
        from_email = parseaddr(record["_from"])[1].lower()
        if from_email and from_email != ctx.world.person(owner).email:
            raise google_error(403, "Invalid From header", reason="forbidden")
        if not addresses(record):
            raise google_error(400, "Recipient address required", reason="invalidArgument")
        from email import policy
        from email.parser import BytesParser

        parsed = BytesParser(policy=policy.SMTP).parsebytes(data)
        if not parsed.get("From"):
            parsed["From"] = ctx.world.person(owner).email
        if not parsed.get("Date"):
            parsed["Date"] = format_datetime(ctx.clock.now())
        if not parsed.get("Message-ID"):
            parsed["Message-ID"] = f"<{message_id}@{ctx.world.org.domain}>"
        data = parsed.as_bytes()
        record = parse_raw(ctx, data, message_id, thread_id, ["SENT"])
    requested = body.get("threadId")
    headers = {k.lower(): v for k, v in record["_headers"]}
    refs = headers.get("in-reply-to", "") + " " + headers.get("references", "")
    existing = [r for r in messages(ctx, owner) if r["_rfcId"] in refs]
    if requested:
        if not any(r["threadId"] == requested for r in messages(ctx, owner)):
            raise google_error(404, "Requested entity was not found.", reason="notFound")
        record["threadId"] = requested
    elif existing:
        record["threadId"] = existing[-1]["threadId"]
    if (
        mode in ("insert", "import")
        and request.query_params.get("internalDateSource", "dateHeader") == "dateHeader"
    ):
        try:
            record["internalDate"] = str(
                int(parsedate_to_datetime(headers.get("date", "")).timestamp() * 1000)
            )
        except (ValueError, TypeError, OverflowError):
            pass
    messages(ctx, owner).put(record)
    await change(ctx, owner, record, "messageAdded")
    if mode == "send":
        for email in dict.fromkeys(addresses(record)):
            person = ctx.world.person_by_email(email)
            if not person or not ctx.store["gmail_mailboxes"].get(person.id):
                continue
            if person.id == owner:
                await modify(ctx, owner, record, {"addLabelIds": ["INBOX"]})
                continue
            replica = copy.deepcopy(record)
            replica["labelIds"] = ["INBOX", "UNREAD"]
            apply_filters(ctx, person.id, replica)
            # Bcc recipients are delivered, but their addresses are never disclosed.
            replica["_headers"] = [(k, v) for k, v in replica["_headers"] if k.lower() != "bcc"]
            replica["_bcc"] = ""
            if "bcc" in headers:
                from email import policy
                from email.parser import BytesParser

                parsed = BytesParser(policy=policy.SMTP).parsebytes(data)
                del parsed["Bcc"]
                replica["_rawBlob"] = ctx.store.blobs.put(parsed.as_bytes())
            messages(ctx, person.id).put(replica)
            await change(ctx, person.id, replica, "messageAdded")
    return record


def apply_filters(ctx: ProviderContext, owner: str, record: dict[str, Any]) -> None:
    """Apply supported inbound criteria without making external forwarding calls."""
    label_names = {r["name"].casefold(): r["id"] for r in labels(ctx, owner)}
    for rule in ctx.store["gmail_filters:" + owner]:
        criteria = rule["criteria"]
        if any(
            str(criteria[field]).casefold() not in record.get("_" + field, "").casefold()
            for field in ("from", "to", "subject")
            if field in criteria
        ):
            continue
        if criteria.get("hasAttachment") and not record.get("_attachments"):
            continue
        if criteria.get("query") and not compile_query(criteria["query"], ctx.clock.now(), label_names)(
            record
        ):
            continue
        if criteria.get("negatedQuery") and compile_query(
            criteria["negatedQuery"], ctx.clock.now(), label_names
        )(record):
            continue
        if criteria.get("size"):
            size = int(criteria["size"])
            if (criteria.get("sizeComparison") == "larger" and record["sizeEstimate"] <= size) or (
                criteria.get("sizeComparison") == "smaller" and record["sizeEstimate"] >= size
            ):
                continue
        action = rule["action"]
        record["labelIds"] = list(
            dict.fromkeys(
                [label for label in record["labelIds"] if label not in action.get("removeLabelIds", [])]
                + action.get("addLabelIds", [])
            )
        )


def page(
    request: Request, rows: list[Any], *, default: int = 100, maximum: int = 500, binding: str = ""
) -> tuple[list[Any], str | None]:
    try:
        size = int(request.query_params.get("maxResults", default))
        if size < 1:
            raise ValueError
        size = min(size, maximum)
        binding = stable_hash(
            request.url.path,
            binding,
            sorted(
                (k, v) for k, v in request.query_params.multi_items() if k not in ("pageToken", "maxResults")
            ),
        )
        token = request.query_params.get("pageToken")
        decoded = json.loads(b64url_decode(token)) if token else [binding, 0]
        if (
            not isinstance(decoded, list)
            or len(decoded) != 2
            or decoded[0] != binding
            or not isinstance(decoded[1], int)
            or decoded[1] < 0
        ):
            raise ValueError
        offset = decoded[1]
    except (ValueError, TypeError, UnicodeError, IndexError) as exc:
        raise google_error(400, "Invalid page token or maxResults", reason="invalidArgument") from exc
    return rows[offset : offset + size], b64url(
        json.dumps([binding, offset + size]).encode()
    ) if offset + size < len(rows) else None
