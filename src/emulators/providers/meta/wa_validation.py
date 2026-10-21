"""WhatsApp payload constraints shared by create and update handlers."""

from __future__ import annotations

from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.meta.common import invalid_param
from emulators.providers.meta.validation import (
    http_url,
    list_value,
    number_value,
    object_value,
    string_value,
)

MEDIA_LIMITS = {
    "audio/aac": 16,
    "audio/mp4": 16,
    "audio/mpeg": 16,
    "audio/amr": 16,
    "audio/ogg": 16,
    "application/pdf": 100,
    "text/plain": 100,
    "application/msword": 100,
    "application/vnd.ms-powerpoint": 100,
    "application/vnd.ms-excel": 100,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": 100,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": 100,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": 100,
    "image/jpeg": 5,
    "image/png": 5,
    "image/webp": 0.5,
    "video/mp4": 16,
    "video/3gpp": 16,
}


def template_components(ctx: ProviderContext, value: Any) -> list[dict[str, Any]]:
    components = list_value(ctx, value, "components", maximum=10)
    if not components:
        invalid_param(ctx, "A template must have a BODY component.")
    seen: set[str] = set()
    for raw in components:
        component = object_value(ctx, raw, "component")
        kind = string_value(ctx, component.get("type"), "component.type")
        if kind not in ("HEADER", "BODY", "FOOTER", "BUTTONS") or kind in seen:
            invalid_param(ctx, "Invalid or duplicate template component type.")
        seen.add(kind)
        if kind in ("BODY", "FOOTER") or (kind == "HEADER" and component.get("format") == "TEXT"):
            string_value(ctx, component.get("text"), "component.text", maximum=1024 if kind == "BODY" else 60)
        if kind == "HEADER" and component.get("format") not in (
            "TEXT",
            "IMAGE",
            "DOCUMENT",
            "VIDEO",
            "LOCATION",
        ):
            invalid_param(ctx, "Invalid template header format.")
        if kind == "BUTTONS":
            for button in list_value(ctx, component.get("buttons"), "buttons", maximum=10):
                button = object_value(ctx, button, "button")
                string_value(ctx, button.get("type"), "button.type")
                string_value(ctx, button.get("text"), "button.text", maximum=25)
    if "BODY" not in seen:
        invalid_param(ctx, "A template must have a BODY component.")
    return components


def media_ref(ctx: ProviderContext, payload: dict[str, Any], kind: str) -> None:
    if bool(payload.get("id")) == bool(payload.get("link")):
        invalid_param(ctx, f"{kind} must contain exactly one of id or link.")
    if payload.get("id"):
        string_value(ctx, payload["id"], f"{kind}.id")
    else:
        http_url(ctx, payload["link"], f"{kind}.link")
    if "caption" in payload:
        if kind not in ("image", "video", "document"):
            invalid_param(ctx, f"{kind} does not support caption.")
        string_value(ctx, payload["caption"], "caption", maximum=1024, empty=True)
    if "filename" in payload:
        string_value(ctx, payload["filename"], "filename", maximum=240)


def send_payload(ctx: ProviderContext, kind: str, payload: Any) -> Any:
    if kind == "contacts":
        contacts = list_value(ctx, payload, "contacts")
        if not contacts:
            invalid_param(ctx, "contacts must not be empty.")
        for raw in contacts:
            contact = object_value(ctx, raw, "contact")
            name = object_value(ctx, contact.get("name"), "contact.name")
            string_value(ctx, name.get("formatted_name"), "contact.name.formatted_name", maximum=512)
            if not any(name.get(k) for k in ("first_name", "last_name", "middle_name", "prefix", "suffix")):
                invalid_param(ctx, "contact.name requires a name component.")
        return contacts
    payload = object_value(ctx, payload, kind)
    if kind == "text":
        string_value(ctx, payload.get("body"), "text.body", maximum=4096)
        if "preview_url" in payload and type(payload["preview_url"]) is not bool:
            invalid_param(ctx, "text.preview_url must be a boolean.")
    elif kind in ("image", "document", "audio", "video", "sticker"):
        media_ref(ctx, payload, kind)
    elif kind == "location":
        number_value(ctx, payload.get("latitude"), "latitude", -90, 90)
        number_value(ctx, payload.get("longitude"), "longitude", -180, 180)
        for key in ("name", "address"):
            if key in payload:
                string_value(ctx, payload[key], key, maximum=1000, empty=True)
    elif kind == "reaction":
        string_value(ctx, payload.get("message_id"), "reaction.message_id")
        string_value(ctx, payload.get("emoji"), "reaction.emoji", maximum=32, empty=True)
    elif kind == "template":
        string_value(ctx, payload.get("name"), "template.name", maximum=512)
        language = object_value(ctx, payload.get("language"), "template.language")
        string_value(ctx, language.get("code"), "template.language.code", maximum=32)
        if language.get("policy", "deterministic") != "deterministic":
            invalid_param(ctx, "template.language.policy must be deterministic.")
        for raw in list_value(ctx, payload.get("components", []), "template.components", maximum=100):
            component = object_value(ctx, raw, "template.component")
            ctype = string_value(ctx, component.get("type"), "component.type")
            if ctype not in ("body", "header", "button"):
                invalid_param(ctx, "Invalid template send component type.")
            for raw_param in list_value(ctx, component.get("parameters", []), "parameters"):
                param = object_value(ctx, raw_param, "parameter")
                ptype = string_value(ctx, param.get("type"), "parameter.type")
                if ptype in ("text", "payload"):
                    string_value(ctx, param.get(ptype), f"parameter.{ptype}", maximum=32768)
                elif ptype in ("image", "video", "document"):
                    media_ref(ctx, object_value(ctx, param.get(ptype), ptype), ptype)
                elif ptype in ("currency", "date_time"):
                    value = object_value(ctx, param.get(ptype), ptype)
                    string_value(ctx, value.get("fallback_value"), f"{ptype}.fallback_value")
                else:
                    invalid_param(ctx, "Invalid template parameter type.")
    elif kind == "interactive":
        itype = string_value(ctx, payload.get("type"), "interactive.type")
        if itype not in ("button", "list"):
            invalid_param(ctx, "Supported interactive types are button and list.")
        if "header" in payload:
            header = object_value(ctx, payload["header"], "interactive.header")
            htype = string_value(ctx, header.get("type"), "interactive.header.type")
            if htype == "text":
                string_value(ctx, header.get("text"), "interactive.header.text", maximum=60)
            elif itype == "button" and htype in ("image", "video", "document"):
                media_ref(ctx, object_value(ctx, header.get(htype), f"header.{htype}"), htype)
            else:
                invalid_param(
                    ctx, "List headers must be text; button headers support text, image, video or document."
                )
        if "footer" in payload:
            footer = object_value(ctx, payload["footer"], "interactive.footer")
            string_value(ctx, footer.get("text"), "interactive.footer.text", maximum=60)
        body = object_value(ctx, payload.get("body"), "interactive.body")
        string_value(ctx, body.get("text"), "interactive.body.text", maximum=1024)
        action = object_value(ctx, payload.get("action"), "interactive.action")
        if itype == "button":
            buttons = list_value(ctx, action.get("buttons"), "buttons", maximum=3)
            if not buttons:
                invalid_param(ctx, "At least one reply button is required.")
            ids = []
            for raw in buttons:
                button = object_value(ctx, raw, "button")
                if button.get("type") != "reply":
                    invalid_param(ctx, "button.type must be reply.")
                reply = object_value(ctx, button.get("reply"), "button.reply")
                ids.append(string_value(ctx, reply.get("id"), "reply.id", maximum=256))
                string_value(ctx, reply.get("title"), "reply.title", maximum=20)
            if len(ids) != len(set(ids)):
                invalid_param(ctx, "Reply button ids must be unique.")
        else:
            string_value(ctx, action.get("button"), "action.button", maximum=20)
            sections = list_value(ctx, action.get("sections"), "sections", maximum=10)
            count = 0
            ids = []
            for raw in sections:
                section = object_value(ctx, raw, "section")
                rows = list_value(ctx, section.get("rows"), "rows", maximum=10)
                if not rows:
                    invalid_param(ctx, "A section must contain rows.")
                count += len(rows)
                for raw_row in rows:
                    row = object_value(ctx, raw_row, "row")
                    ids.append(string_value(ctx, row.get("id"), "row.id", maximum=200))
                    string_value(ctx, row.get("title"), "row.title", maximum=24)
                    if "description" in row:
                        string_value(ctx, row["description"], "row.description", maximum=72, empty=True)
            if not sections or count > 10 or len(ids) != len(set(ids)):
                invalid_param(ctx, "A list must have 1-10 rows with unique ids.")
    return payload
