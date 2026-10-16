"""Gmail v1 mailboxes, labels, drafts, history, watches and settings."""

from __future__ import annotations

import copy
import re
from typing import Any

from fastapi import FastAPI, Request, Response

from emulators.core.ids import b64url
from emulators.core.provider import ProviderContext
from emulators.providers.google.common import google_error, json_object
from emulators.providers.google.gmail_mime import (
    attachment_bytes,
    decode_raw,
    message_output,
    parse_raw,
    read_upload,
)
from emulators.providers.google.gmail_query import compile_query
from emulators.providers.google.gmail_state import (
    change,
    get_message,
    ingest,
    labels,
    mailbox,
    messages,
    modify,
    page,
    push,
    reference,
    remove_message,
    seed,
    validate_labels,
)

__all__ = ["register", "seed"]
PREFIX = "/gmail/v1/users/{user_id}"


def _thread(ctx: ProviderContext, owner: str, thread_id: str) -> list[dict[str, Any]]:
    rows = messages(ctx, owner).where(threadId=thread_id)
    if not rows:
        raise google_error(404, "Requested entity was not found.", reason="notFound")
    return sorted(rows, key=lambda r: int(r["internalDate"]))


def _thread_output(request: Request, ctx: ProviderContext, owner: str, thread_id: str) -> dict[str, Any]:
    rows = _thread(ctx, owner, thread_id)
    return {
        "id": thread_id,
        "snippet": rows[-1]["snippet"],
        "historyId": str(max(int(r["historyId"]) for r in rows)),
        "messages": [
            message_output(
                ctx,
                r,
                request.query_params.get("format", "full"),
                request.query_params.getlist("metadataHeaders"),
            )
            for r in rows
        ],
    }


def _filtered(request: Request, ctx: ProviderContext, owner: str) -> list[dict[str, Any]]:
    predicate = compile_query(
        request.query_params.get("q", ""),
        ctx.clock.now(),
        {r["name"].casefold(): r["id"] for r in labels(ctx, owner)},
    )
    required = request.query_params.getlist("labelIds")
    include = request.query_params.get("includeSpamTrash", "false") == "true"
    include = include or bool(
        re.search(
            r"(?<![-\w])(?:in|label):(?:anywhere|trash|spam)\b",
            request.query_params.get("q", ""),
            re.IGNORECASE,
        )
    )
    rows = [
        r
        for r in messages(ctx, owner)
        if (include or not {"TRASH", "SPAM"}.intersection(r["labelIds"]))
        and all(label in r["labelIds"] for label in required)
        and predicate(r)
    ]
    return sorted(rows, key=lambda r: int(r["internalDate"]), reverse=True)


def _label(ctx: ProviderContext, owner: str, label_id: str) -> dict[str, Any]:
    label = labels(ctx, owner).get(label_id)
    if not label:
        raise google_error(404, "Not Found", reason="notFound")
    return label


def _label_output(ctx: ProviderContext, owner: str, label: dict[str, Any]) -> dict[str, Any]:
    rows = [r for r in messages(ctx, owner) if label["id"] in r["labelIds"]]
    unread = [r for r in rows if "UNREAD" in r["labelIds"]]
    return {
        **copy.deepcopy(label),
        "messagesTotal": len(rows),
        "messagesUnread": len(unread),
        "threadsTotal": len({r["threadId"] for r in rows}),
        "threadsUnread": len({r["threadId"] for r in unread}),
    }


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get(PREFIX + "/profile")
    async def profile(request: Request, user_id: str) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        rows = messages(ctx, owner).values()
        return {
            "emailAddress": ctx.world.person(owner).email,
            "messagesTotal": len(rows),
            "threadsTotal": len({r["threadId"] for r in rows}),
            "historyId": str(ctx.store["gmail_mailboxes"].get(owner)["historyId"]),
        }

    @app.get(PREFIX + "/messages")
    async def messages_list(request: Request, user_id: str) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        rows = _filtered(request, ctx, owner)
        result: dict[str, Any] = {"resultSizeEstimate": len(rows)}
        rows, token = page(request, rows, binding=owner)
        if rows:
            result["messages"] = [reference(r, with_labels=False) for r in rows]
        if token:
            result["nextPageToken"] = token
        return result

    @app.post(PREFIX + "/messages/send")
    @app.post(PREFIX + "/messages/insert")
    @app.post(PREFIX + "/messages/import")
    @app.post(PREFIX + "/messages")
    @app.post("/upload" + PREFIX + "/messages/send")
    @app.post("/upload" + PREFIX + "/messages/import")
    @app.post("/upload" + PREFIX + "/messages")
    async def messages_create(request: Request, user_id: str) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        mode = request.url.path.rsplit("/", 1)[-1]
        mode = "insert" if mode == "messages" else mode
        record = await ingest(request, ctx, owner, await read_upload(request), mode)
        return reference(record)

    @app.post(PREFIX + "/messages/batchModify")
    @app.post(PREFIX + "/messages/batchDelete")
    async def messages_batch(request: Request, user_id: str) -> Response:
        owner = mailbox(request, ctx, user_id)
        body = await json_object(request)
        if not isinstance(body.get("ids"), list) or len(body["ids"]) > 1000:
            raise google_error(400, "Invalid message ids", reason="invalidArgument")
        if request.url.path.endswith("batchModify"):
            validate_labels(ctx, owner, body.get("addLabelIds", []))
            validate_labels(ctx, owner, body.get("removeLabelIds", []))
        for message_id in body["ids"]:
            record = messages(ctx, owner).get(message_id)
            if record:
                if request.url.path.endswith("batchDelete"):
                    await remove_message(ctx, owner, record)
                else:
                    await modify(ctx, owner, record, body)
        return Response(status_code=204)

    @app.api_route(PREFIX + "/messages/{message_id}", methods=["GET", "DELETE"])
    async def messages_item(request: Request, user_id: str, message_id: str) -> Any:
        owner = mailbox(request, ctx, user_id)
        record = get_message(ctx, owner, message_id)
        if request.method == "DELETE":
            await remove_message(ctx, owner, record)
            return Response(status_code=204)
        return message_output(
            ctx,
            record,
            request.query_params.get("format", "full"),
            request.query_params.getlist("metadataHeaders"),
        )

    @app.get(PREFIX + "/messages/{message_id}/attachments/{attachment_id}")
    async def attachments_get(
        request: Request, user_id: str, message_id: str, attachment_id: str
    ) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        record = get_message(ctx, owner, message_id)
        attachment = next((a for a in record["_attachments"] if a["attachmentId"] == attachment_id), None)
        if attachment is None:
            raise google_error(404, "Not Found", reason="notFound")
        data = attachment_bytes(ctx, attachment)
        return {"size": len(data), "data": b64url(data)}

    @app.post(PREFIX + "/messages/{message_id}/{operation}")
    async def messages_operation(
        request: Request, user_id: str, message_id: str, operation: str
    ) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        record = get_message(ctx, owner, message_id)
        if operation == "modify":
            await modify(ctx, owner, record, await json_object(request))
        elif operation in ("trash", "untrash"):
            await modify(
                ctx,
                owner,
                record,
                {"addLabelIds": ["TRASH"]} if operation == "trash" else {"removeLabelIds": ["TRASH"]},
            )
        else:
            raise google_error(404, "Not Found", reason="notFound")
        return reference(record)

    @app.get(PREFIX + "/threads")
    async def threads_list(request: Request, user_id: str) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        threads = {}
        for record in _filtered(request, ctx, owner):
            threads.setdefault(
                record["threadId"],
                {"id": record["threadId"], "snippet": record["snippet"], "historyId": record["historyId"]},
            )
            threads[record["threadId"]]["historyId"] = str(
                max(int(threads[record["threadId"]]["historyId"]), int(record["historyId"]))
            )
        rows, token = page(request, list(threads.values()), binding=owner)
        return {
            "resultSizeEstimate": len(threads),
            **({"threads": rows} if rows else {}),
            **({"nextPageToken": token} if token else {}),
        }

    @app.api_route(PREFIX + "/threads/{thread_id}", methods=["GET", "DELETE"])
    async def threads_item(request: Request, user_id: str, thread_id: str) -> Any:
        owner = mailbox(request, ctx, user_id)
        if request.method == "DELETE":
            for record in _thread(ctx, owner, thread_id):
                await remove_message(ctx, owner, record)
            return Response(status_code=204)
        return _thread_output(request, ctx, owner, thread_id)

    @app.post(PREFIX + "/threads/{thread_id}/{operation}")
    async def threads_operation(
        request: Request, user_id: str, thread_id: str, operation: str
    ) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        records = _thread(ctx, owner, thread_id)
        if operation not in ("modify", "trash", "untrash"):
            raise google_error(404, "Not Found", reason="notFound")
        body = (
            await json_object(request)
            if operation == "modify"
            else ({"addLabelIds": ["TRASH"]} if operation == "trash" else {"removeLabelIds": ["TRASH"]})
        )
        validate_labels(ctx, owner, body.get("addLabelIds", []))
        validate_labels(ctx, owner, body.get("removeLabelIds", []))
        for record in records:
            await modify(ctx, owner, record, body)
        return _thread_output(request, ctx, owner, thread_id)

    @app.get(PREFIX + "/labels")
    async def labels_list(request: Request, user_id: str) -> dict[str, Any]:
        return {"labels": copy.deepcopy(labels(ctx, mailbox(request, ctx, user_id)).values())}

    @app.post(PREFIX + "/labels")
    async def labels_create(request: Request, user_id: str) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        body = await json_object(request)
        name = body.get("name", "")
        if not name or any(r["name"].casefold() == name.casefold() for r in labels(ctx, owner)):
            raise google_error(400, "Invalid label name or name exists", reason="invalidArgument")
        number = (
            max([int(r["id"].split("_")[1]) for r in labels(ctx, owner) if r["type"] == "user"] or [0]) + 1
        )
        label = {
            "id": f"Label_{number}",
            "name": name,
            "type": "user",
            "messageListVisibility": body.get("messageListVisibility", "show"),
            "labelListVisibility": body.get("labelListVisibility", "labelShow"),
        }
        if "color" in body:
            label["color"] = body["color"]
        labels(ctx, owner).put(label)
        return label

    @app.api_route(PREFIX + "/labels/{label_id}", methods=["GET", "PATCH", "PUT", "DELETE"])
    async def labels_item(request: Request, user_id: str, label_id: str) -> Any:
        owner = mailbox(request, ctx, user_id)
        label = _label(ctx, owner, label_id)
        if request.method == "GET":
            return _label_output(ctx, owner, label)
        if label["type"] == "system" and request.method == "DELETE":
            raise google_error(400, "Cannot delete a system label", reason="invalidArgument")
        if request.method == "DELETE":
            for record in messages(ctx, owner):
                if label_id in record["labelIds"]:
                    await modify(ctx, owner, record, {"removeLabelIds": [label_id]})
            labels(ctx, owner).delete(label_id)
            return Response(status_code=204)
        body = await json_object(request)
        if "name" in body and (
            label["type"] == "system"
            or any(
                r["id"] != label_id and r["name"].casefold() == body["name"].casefold()
                for r in labels(ctx, owner)
            )
        ):
            raise google_error(400, "Invalid label name", reason="invalidArgument")
        for key in ("name", "color", "messageListVisibility", "labelListVisibility"):
            if key in body:
                label[key] = body[key]
        return copy.deepcopy(label)

    @app.get(PREFIX + "/drafts")
    async def drafts_list(request: Request, user_id: str) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        matching = {r["id"] for r in _filtered(request, ctx, owner)}
        rows = [r for r in ctx.store["gmail_drafts:" + owner] if r["messageId"] in matching]
        total = len(rows)
        rows, token = page(request, rows, binding=owner)
        return {
            "resultSizeEstimate": total,
            **(
                {
                    "drafts": [
                        {
                            "id": r["id"],
                            "message": reference(get_message(ctx, owner, r["messageId"]), with_labels=False),
                        }
                        for r in rows
                    ]
                }
                if rows
                else {}
            ),
            **({"nextPageToken": token} if token else {}),
        }

    async def draft_save(request: Request, user_id: str, draft_id: str | None = None) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        body = await read_upload(request)
        body = body if "message" in body else {"message": body}
        raw = decode_raw(body["message"])
        draft = ctx.store["gmail_drafts:" + owner].get(draft_id) if draft_id else None
        if draft_id and not draft:
            raise google_error(404, "Not Found", reason="notFound")
        record = parse_raw(
            ctx, raw, ctx.ids.hex(16), body["message"].get("threadId", ctx.ids.hex(16)), ["DRAFT"]
        )
        if draft:
            await remove_message(ctx, owner, get_message(ctx, owner, draft["messageId"]))
        draft_id = draft_id or "r" + ctx.ids.digits(19)
        messages(ctx, owner).put(record)
        ctx.store["gmail_drafts:" + owner].put({"id": draft_id, "messageId": record["id"]})
        await change(ctx, owner, record, "messageAdded")
        return {"id": draft_id, "message": message_output(ctx, record)}

    @app.post(PREFIX + "/drafts")
    @app.post("/upload" + PREFIX + "/drafts")
    async def drafts_create(request: Request, user_id: str) -> dict[str, Any]:
        return await draft_save(request, user_id)

    @app.post(PREFIX + "/drafts/send")
    @app.post("/upload" + PREFIX + "/drafts/send")
    async def drafts_send(request: Request, user_id: str) -> dict[str, Any]:
        from emulators.providers.google.gmail_mime import raw_bytes

        owner = mailbox(request, ctx, user_id)
        body = await read_upload(request)
        draft = ctx.store["gmail_drafts:" + owner].get(body.get("id"))
        if not draft:
            raise google_error(404, "Not Found", reason="notFound")
        old = get_message(ctx, owner, draft["messageId"])
        sent_body = body.get("message", {})
        sent_body.setdefault("raw", b64url(raw_bytes(ctx, old)))
        if any(r["threadId"] == old["threadId"] and r["id"] != old["id"] for r in messages(ctx, owner)):
            sent_body.setdefault("threadId", old["threadId"])
        record = await ingest(request, ctx, owner, sent_body, "send")
        await remove_message(ctx, owner, old)
        ctx.store["gmail_drafts:" + owner].delete(draft["id"])
        return reference(record)

    @app.api_route(PREFIX + "/drafts/{draft_id}", methods=["GET", "PUT", "DELETE"])
    @app.put("/upload" + PREFIX + "/drafts/{draft_id}")
    async def drafts_item(request: Request, user_id: str, draft_id: str) -> Any:
        if request.method == "PUT":
            return await draft_save(request, user_id, draft_id)
        owner = mailbox(request, ctx, user_id)
        draft = ctx.store["gmail_drafts:" + owner].get(draft_id)
        if not draft:
            raise google_error(404, "Not Found", reason="notFound")
        record = get_message(ctx, owner, draft["messageId"])
        if request.method == "DELETE":
            await remove_message(ctx, owner, record)
            ctx.store["gmail_drafts:" + owner].delete(draft_id)
            return Response(status_code=204)
        return {
            "id": draft_id,
            "message": message_output(
                ctx,
                record,
                request.query_params.get("format", "full"),
                request.query_params.getlist("metadataHeaders"),
            ),
        }

    @app.get(PREFIX + "/history")
    async def history_list(request: Request, user_id: str) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        query = request.query_params
        box = ctx.store["gmail_mailboxes"].get(owner)
        try:
            start = int(query["startHistoryId"])
        except (KeyError, ValueError) as exc:
            raise google_error(400, "startHistoryId is required", reason="invalidArgument") from exc
        if start < box["floor"]:
            raise google_error(404, "Requested entity was not found.", reason="notFound")
        kinds = {
            "messageAdded": "messagesAdded",
            "messageDeleted": "messagesDeleted",
            "labelAdded": "labelsAdded",
            "labelRemoved": "labelsRemoved",
        }
        wanted = query.getlist("historyTypes")
        if any(t not in kinds for t in wanted):
            raise google_error(400, "Invalid history type", reason="invalidArgument")
        rows = [
            r
            for r in ctx.store["gmail_history:" + owner]
            if int(r["id"]) > start
            and (not wanted or any(kinds[t] in r for t in wanted))
            and (not query.get("labelId") or query["labelId"] in r["_labels"])
        ]
        rows, token = page(request, rows, default=100, maximum=500, binding=owner)
        rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
        return {
            "historyId": str(box["historyId"]),
            **({"history": rows} if rows else {}),
            **({"nextPageToken": token} if token else {}),
        }

    @app.post(PREFIX + "/watch")
    async def watch(request: Request, user_id: str) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        body = await json_object(request)
        if not body.get("topicName", "").startswith("projects/") or "/topics/" not in body["topicName"]:
            raise google_error(400, "Invalid topicName", reason="invalidArgument")
        behavior = body.get("labelFilterBehavior", body.get("labelFilterAction", "include"))
        if behavior not in ("include", "exclude"):
            raise google_error(400, "Invalid labelFilterBehavior", reason="invalidArgument")
        validate_labels(ctx, owner, body.get("labelIds", []))
        expiration = (ctx.clock.timestamp() + 604800) * 1000
        ctx.store["gmail_watches"].put(
            {
                "id": owner,
                "topicName": body["topicName"],
                "labelIds": body.get("labelIds", []),
                "labelFilterBehavior": behavior,
                "expires": expiration,
            }
        )
        await push(ctx, owner, body.get("labelIds", []))
        return {
            "historyId": str(ctx.store["gmail_mailboxes"].get(owner)["historyId"]),
            "expiration": str(expiration),
        }

    @app.post(PREFIX + "/stop")
    async def stop(request: Request, user_id: str) -> Response:
        ctx.store["gmail_watches"].delete(mailbox(request, ctx, user_id))
        return Response(status_code=204)

    @app.get(PREFIX + "/settings/sendAs")
    async def send_as(request: Request, user_id: str) -> dict[str, Any]:
        person = ctx.world.person(mailbox(request, ctx, user_id))
        return {
            "sendAs": [
                {
                    "sendAsEmail": person.email,
                    "displayName": person.display_name,
                    "replyToAddress": person.email,
                    "signature": "",
                    "isPrimary": True,
                    "isDefault": True,
                    "treatAsAlias": False,
                    "verificationStatus": "accepted",
                }
            ]
        }

    @app.get(PREFIX + "/settings/vacation")
    async def vacation(request: Request, user_id: str) -> dict[str, Any]:
        mailbox(request, ctx, user_id)
        return {"enableAutoReply": False, "restrictToContacts": False, "restrictToDomain": False}

    @app.api_route(PREFIX + "/settings/filters", methods=["GET", "POST"])
    async def filters(request: Request, user_id: str) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        if request.method == "GET":
            return {"filter": copy.deepcopy(ctx.store["gmail_filters:" + owner].values())}
        body = await json_object(request)
        if not body.get("criteria") or not body.get("action"):
            raise google_error(400, "criteria and action are required", reason="invalidArgument")
        for field in ("addLabelIds", "removeLabelIds"):
            validate_labels(ctx, owner, body["action"].get(field, []))
        record = {"id": ctx.ids.urlsafe(24), "criteria": body["criteria"], "action": body["action"]}
        ctx.store["gmail_filters:" + owner].put(record)
        return record

    async def configure_push(action_ctx: ProviderContext, body: dict[str, Any]) -> dict[str, Any]:
        """Connect a synthetic Pub/Sub topic to a test push receiver."""
        if not body.get("topicName") or not body.get("endpoint"):
            raise google_error(400, "topicName and endpoint required", reason="invalidArgument")
        action_ctx.store["gmail_pubsub"].put({"id": body["topicName"], "endpoint": body["endpoint"]})
        return {"topicName": body["topicName"], "endpoint": body["endpoint"]}

    ctx.provider.actions["configure_pubsub_push"] = configure_push
