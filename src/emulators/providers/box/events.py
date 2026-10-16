"""Event log (``GET /events``) and webhook v2 fan-out.

Every mutating route records one :class:`event` and, when a webhook watches the item, a signed
delivery. Both are produced by :func:`emit` so the user event stream, the enterprise
``admin_logs`` stream and webhooks can never disagree about what happened.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.core.webhooks import hmac_sha256_b64
from emulators.providers.box import db, tree
from emulators.providers.box.access import Principal
from emulators.providers.box.shapes import Shaper
from emulators.providers.box.util import box_ts, now_iso, random_lower, to_iso

Record = dict[str, Any]

#: (event type, item type) -> webhook trigger.
TRIGGERS: dict[tuple[str, str], str] = {
    ("ITEM_UPLOAD", "file"): "FILE.UPLOADED",
    ("ITEM_PREVIEW", "file"): "FILE.PREVIEWED",
    ("ITEM_DOWNLOAD", "file"): "FILE.DOWNLOADED",
    ("ITEM_DOWNLOAD", "folder"): "FOLDER.DOWNLOADED",
    ("ITEM_TRASH", "file"): "FILE.TRASHED",
    ("ITEM_TRASH", "folder"): "FOLDER.TRASHED",
    ("DELETE", "file"): "FILE.DELETED",
    ("DELETE", "folder"): "FOLDER.DELETED",
    ("ITEM_UNDELETE_VIA_TRASH", "file"): "FILE.RESTORED",
    ("ITEM_UNDELETE_VIA_TRASH", "folder"): "FOLDER.RESTORED",
    ("ITEM_COPY", "file"): "FILE.COPIED",
    ("ITEM_COPY", "folder"): "FOLDER.COPIED",
    ("ITEM_MOVE", "file"): "FILE.MOVED",
    ("ITEM_MOVE", "folder"): "FOLDER.MOVED",
    ("ITEM_RENAME", "file"): "FILE.RENAMED",
    ("ITEM_RENAME", "folder"): "FOLDER.RENAMED",
    ("LOCK", "file"): "FILE.LOCKED",
    ("UNLOCK", "file"): "FILE.UNLOCKED",
    ("ITEM_CREATE", "folder"): "FOLDER.CREATED",
    ("COMMENT_CREATE", "comment"): "COMMENT.CREATED",
    ("COMMENT_DELETE", "comment"): "COMMENT.DELETED",
    ("TASK_ASSIGNMENT_CREATE", "task_assignment"): "TASK_ASSIGNMENT.CREATED",
    ("TASK_ASSIGNMENT_UPDATE", "task_assignment"): "TASK_ASSIGNMENT.UPDATED",
    ("METADATA_INSTANCE_CREATE", "metadata"): "METADATA_INSTANCE.CREATED",
    ("METADATA_INSTANCE_UPDATE", "metadata"): "METADATA_INSTANCE.UPDATED",
    ("METADATA_INSTANCE_DELETE", "metadata"): "METADATA_INSTANCE.DELETED",
    ("COLLAB_ADD_COLLABORATOR", "collaboration"): "COLLABORATION.CREATED",
    ("COLLAB_INVITE_COLLABORATOR", "collaboration"): "COLLABORATION.CREATED",
    ("COLLAB_ROLE_CHANGE", "collaboration"): "COLLABORATION.UPDATED",
    ("COLLAB_REMOVE_COLLABORATOR", "collaboration"): "COLLABORATION.REMOVED",
    ("COLLABORATION_ACCEPT", "collaboration"): "COLLABORATION.ACCEPTED",
    ("ITEM_SHARED_CREATE", "file"): "SHARED_LINK.CREATED",
    ("ITEM_SHARED_CREATE", "folder"): "SHARED_LINK.CREATED",
    ("ITEM_SHARED_UPDATE", "file"): "SHARED_LINK.UPDATED",
    ("ITEM_SHARED_UPDATE", "folder"): "SHARED_LINK.UPDATED",
    ("ITEM_SHARED_UNSHARE", "file"): "SHARED_LINK.DELETED",
    ("ITEM_SHARED_UNSHARE", "folder"): "SHARED_LINK.DELETED",
}

#: Triggers Box accepts that no stream event implies (they are fired with an explicit ``trigger``).
EXTRA_TRIGGERS = {
    "COMMENT.UPDATED", "COLLABORATION.REJECTED", "WEBHOOK.DELETED",
    # Box Sign is not emulated, but its triggers are valid subscriptions that simply never fire.
    "SIGN_REQUEST.COMPLETED", "SIGN_REQUEST.DECLINED", "SIGN_REQUEST.EXPIRED",
    "SIGN_REQUEST.SIGNER_EMAIL_BOUNCED", "SIGN_REQUEST.SIGN_DOCUMENT_CREATED",
    "SIGN_REQUEST.SIGN_ERROR_FINALIZING", "SIGN_REQUEST.SIGN_SIGNER_SIGNED",
}  # fmt: skip
ALL_TRIGGERS = sorted(set(TRIGGERS.values()) | EXTRA_TRIGGERS)

#: Events that change the file tree: the ``changes`` and ``sync`` streams only carry these.
CHANGE_EVENTS = {
    "ITEM_CREATE", "ITEM_UPLOAD", "ITEM_MOVE", "ITEM_COPY", "ITEM_TRASH", "ITEM_UNDELETE_VIA_TRASH",
    "ITEM_RENAME", "DELETE", "COLLAB_ADD_COLLABORATOR", "COLLAB_INVITE_COLLABORATOR",
    "COLLAB_REMOVE_COLLABORATOR", "COLLAB_ROLE_CHANGE", "COLLABORATION_ACCEPT", "ITEM_SHARED_CREATE",
    "ITEM_SHARED_UPDATE", "ITEM_SHARED_UNSHARE", "LOCK", "UNLOCK",
}  # fmt: skip
EVENT_POS_KEY = "event_pos"

#: The ``admin_logs`` stream names file activity differently from the per-user streams
#: (``UPLOAD`` rather than ``ITEM_UPLOAD``, ``COLLABORATION_ACCEPT`` rather than
#: ``COLLAB_ADD_COLLABORATOR``...).
ADMIN_EVENT_NAMES: dict[str, str] = {
    "ITEM_UPLOAD": "UPLOAD",
    "ITEM_DOWNLOAD": "DOWNLOAD",
    "ITEM_PREVIEW": "PREVIEW",
    "ITEM_COPY": "COPY",
    "ITEM_MOVE": "MOVE",
    "ITEM_RENAME": "RENAME",
    "ITEM_TRASH": "DELETE",
    "ITEM_UNDELETE_VIA_TRASH": "UNDELETE",
    "ITEM_SHARED_CREATE": "SHARE",
    "ITEM_SHARED_UNSHARE": "UNSHARE",
    "COLLAB_INVITE_COLLABORATOR": "COLLABORATION_INVITE",
    "COLLAB_ADD_COLLABORATOR": "COLLABORATION_ACCEPT",
    "COLLAB_REMOVE_COLLABORATOR": "COLLABORATION_REMOVE",
    "COLLAB_ROLE_CHANGE": "COLLABORATION_ROLE_CHANGE",
}


#: Enterprise-level activity that only ever shows up in ``admin_logs``, never in a user's stream.
ENTERPRISE_ONLY = frozenset(
    {
        "NEW_USER", "EDIT_USER", "DELETE_USER", "GROUP_CREATION", "GROUP_DELETION", "GROUP_EDITED",
        "GROUP_ADD_USER", "GROUP_REMOVE_USER", "METADATA_TEMPLATE_CREATE", "METADATA_TEMPLATE_UPDATE",
        "METADATA_TEMPLATE_DELETE", "WATERMARK_LABEL_CREATE", "WATERMARK_LABEL_DELETE", "LOGIN",
        "USER_AUTHENTICATE_OAUTH2_ACCESS_TOKEN_CREATE",
    }
)  # fmt: skip


def admin_name(event_type: str) -> str:
    return ADMIN_EVENT_NAMES.get(event_type, event_type)


def current_position(ctx: ProviderContext) -> int:
    return int(ctx.store.meta.get(EVENT_POS_KEY, 0))


def webhook_keys(ctx: ProviderContext) -> dict[str, str]:
    return ctx.store.meta.setdefault("webhook_keys", {"primary": "", "secondary": ""})


def append_event(
    ctx: ProviderContext,
    event_type: str,
    *,
    created_by: str | None,
    source: Any,
    item: Record | None = None,
    created_at: str | None = None,
    additional_details: Any = None,
    event_id: str | None = None,
    session_id: str | None = None,
    lazy: tuple[str, str] | None = None,
    admin_only: bool = False,
) -> Record:
    """Write one event to the log; returns the stored record. Seeding passes stable ids."""
    pos = current_position(ctx) + 1
    admin_only = admin_only or event_type in ENTERPRISE_ONLY
    ctx.store.meta[EVENT_POS_KEY] = pos
    stamp = created_at or now_iso(ctx)
    owner = item.get("_owner") if item else None
    event = {
        "id": f"ev{pos}",
        "type": "event",
        "event_id": event_id or str(uuid.uuid4()),
        "created_by": created_by,
        "created_at": stamp,
        "recorded_at": stamp,
        "event_type": event_type,
        "session_id": session_id or random_lower(12),
        "source": source,
        "additional_details": additional_details,
        "ip_address": "10.0.0.1",
        "_lazy": lazy,
        "_admin_only": admin_only,
        "_pos": pos,
        "_item_type": item["type"] if item else None,
        "_item_id": item["id"] if item else None,
        "_owner": owner,
    }
    ctx.store.collection(db.EVENTS).put(event)
    return event


def lazy_source(shaper: Shaper, lazy: tuple[str, str]) -> Any:
    """Seeded events keep a reference instead of a snapshot (cheap resets); render it on read."""
    kind, ref = lazy
    ctx = shaper.ctx
    if kind == "collaboration":
        collab = db.collabs(ctx).get(ref)
        return shaper.collaboration(collab) if collab else {"type": "collaboration", "id": ref}
    record = db.item(ctx, kind, ref)
    return item_source(shaper.p, record) if record else {"type": kind, "id": ref}


def render_event(shaper: Shaper, event: Record, *, admin: bool = False) -> Record:
    out: Record = {
        "type": "event",
        "event_id": event["event_id"],
        "created_by": shaper.user_mini(event["created_by"]),
        "created_at": box_ts(event["created_at"]),
        "recorded_at": box_ts(event["recorded_at"]),
        "event_type": admin_name(event["event_type"]) if admin else event["event_type"],
        "session_id": event["session_id"],
        "source": lazy_source(shaper, event["_lazy"]) if event.get("_lazy") else event["source"],
    }
    if event.get("additional_details") is not None:
        out["additional_details"] = event["additional_details"]
    return out


def event_visible(p: Principal, event: Record) -> bool:
    """User streams carry events the caller caused or can currently see (never audit-only events)."""
    if event.get("_admin_only"):
        return False
    if event["created_by"] == p.uid or event.get("_owner") == p.uid:
        return True
    if event["_item_type"] in ("file", "folder"):
        record = db.item(p.ctx, event["_item_type"], event["_item_id"])
        return bool(record and p.visible(record))
    return False


def item_source(p: Principal, record: Record) -> Record:
    shaper = Shaper(p)
    if record["type"] == "folder":
        names = [n for n in ("type", "id", "sequence_id", "etag", "name", "created_at", "modified_at", "description",
                             "size", "path_collection", "created_by", "modified_by", "trashed_at", "purged_at",
                             "content_created_at", "content_modified_at", "owned_by", "shared_link", "parent",
                             "item_status")]  # fmt: skip
        return shaper.folder(record, names)
    return shaper.item(record)


async def emit(
    p: Principal,
    event_type: str,
    record: Record | None,
    *,
    source: Any = None,
    kind: str | None = None,
    additional_details: Any = None,
    webhook_item: Record | None = None,
    trigger: str | None = None,
    log: bool = True,
    admin_only: bool = False,
) -> Record | None:
    """Record an event and fire matching webhooks.

    ``record`` is the file/folder the event is about (also used for webhook scoping);
    ``source`` overrides the default rendering of ``record`` (collaborations, comments...);
    ``kind`` is the source kind for trigger lookup when it is not the item type; ``trigger``
    names the webhook trigger when it is not implied by the event type; ``log=False`` fires
    the webhook for something Box does not write to the event stream (a comment edit).
    """
    ctx = p.ctx
    body = source if source is not None else (item_source(p, record) if record is not None else None)
    event = (
        append_event(
            ctx,
            event_type,
            created_by=p.uid,
            source=body,
            item=record or webhook_item,
            additional_details=additional_details,
            admin_only=admin_only,
        )
        if log
        else None
    )
    trigger = trigger or TRIGGERS.get((event_type, kind or (record["type"] if record else "")))
    scope_item = webhook_item or record
    if trigger and scope_item is not None:
        await fire_webhooks(ctx, trigger, scope_item, body, p.uid, event)
    return event


def watches(ctx: ProviderContext, webhook: Record, item: Record) -> bool:
    target = webhook["target"]
    if target["type"] == "file":
        return item["type"] == "file" and item["id"] == target["id"]
    if target["id"] == db.ROOT_WIRE_ID:
        return item.get("_owner") == webhook["created_by"]
    return tree.is_within(ctx, item, target["id"]) if item["type"] in ("file", "folder") else False


async def fire_webhooks(
    ctx: ProviderContext, trigger: str, item: Record, source: Any, actor_id: str, event: Record | None
) -> None:
    for webhook in list(ctx.store.collection(db.WEBHOOKS)):
        if trigger not in webhook["triggers"] or not watches(ctx, webhook, item):
            continue
        await deliver(ctx, webhook, trigger, source, actor_id)


async def deliver(ctx: ProviderContext, webhook: Record, trigger: str, source: Any, actor_id: str) -> Record:
    delivery_id = str(uuid.uuid4())
    creator = db.users(ctx).get(actor_id)
    created = ctx.clock.now()
    payload = {
        "type": "webhook_event",
        "id": delivery_id,
        "created_at": box_ts(to_iso(created)),
        "trigger": trigger,
        "webhook": {"id": webhook["id"], "type": "webhook"},
        "created_by": {"type": "user", "id": actor_id, "name": creator["name"], "login": creator["login"]}
        if creator
        else None,
        "source": source,
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    keys = webhook_keys(ctx)

    def sign(raw: bytes, attempt: int) -> dict[str, str]:
        stamp = box_ts(to_iso(ctx.clock.now())) or ""  # Box sends ``2020-01-01T00:00:00-07:00``
        headers = {
            "BOX-DELIVERY-ID": delivery_id,
            "BOX-DELIVERY-TIMESTAMP": stamp,
            "BOX-SIGNATURE-ALGORITHM": "HmacSHA256",
            "BOX-SIGNATURE-VERSION": "1",
        }
        message = raw + stamp.encode()
        if keys.get("primary"):
            headers["BOX-SIGNATURE-PRIMARY"] = hmac_sha256_b64(keys["primary"], message)
        if keys.get("secondary"):
            headers["BOX-SIGNATURE-SECONDARY"] = hmac_sha256_b64(keys["secondary"], message)
        return headers

    return await ctx.webhooks.deliver(
        webhook["address"],
        body,
        headers={"User-Agent": "Box-Webhooks/2.0"},
        event_type=trigger,
        sign=sign,
        metadata={"webhook_id": webhook["id"], "delivery_id": delivery_id},
    )
