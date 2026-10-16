"""Messenger message and profile validation without coercing invalid payloads."""

from __future__ import annotations

from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.meta.common import invalid_param
from emulators.providers.meta.validation import http_url, list_value, object_value, string_value


def attachment_value(ctx: ProviderContext, raw: Any, *, reusable: bool = False) -> dict[str, Any]:
    attachment = object_value(ctx, raw, "attachment")
    kind = string_value(ctx, attachment.get("type"), "attachment.type")
    if kind not in ("image", "audio", "video", "file", "template") or (reusable and kind == "template"):
        invalid_param(ctx, "Invalid attachment type.")
    payload = object_value(ctx, attachment.get("payload"), "attachment.payload")
    if kind == "template":
        ttype = string_value(ctx, payload.get("template_type"), "template_type")
        if ttype == "button":
            string_value(ctx, payload.get("text"), "template.text", maximum=640)
            buttons = list_value(ctx, payload.get("buttons"), "buttons", maximum=3)
            if not buttons:
                invalid_param(ctx, "At least one button is required.")
            for button in buttons:
                button_value(ctx, button)
        elif ttype == "generic":
            elements = list_value(ctx, payload.get("elements"), "elements", maximum=10)
            if not elements:
                invalid_param(ctx, "At least one generic template element is required.")
            for raw_element in elements:
                element = object_value(ctx, raw_element, "element")
                string_value(ctx, element.get("title"), "element.title", maximum=80)
                for button in list_value(ctx, element.get("buttons", []), "buttons", maximum=3):
                    button_value(ctx, button)
        else:
            invalid_param(ctx, "Supported template types are button and generic.")
    else:
        if bool(payload.get("url")) == bool(payload.get("attachment_id")):
            invalid_param(ctx, "attachment.payload requires exactly one of url or attachment_id.")
        if payload.get("url"):
            http_url(ctx, payload["url"], "attachment.payload.url")
        if payload.get("attachment_id"):
            string_value(ctx, payload["attachment_id"], "attachment_id")
        if reusable and not payload.get("url"):
            invalid_param(ctx, "attachment.payload.url is required.")
        if "is_reusable" in payload and type(payload["is_reusable"]) is not bool:
            invalid_param(ctx, "is_reusable must be a boolean.")
    return attachment


def button_value(ctx: ProviderContext, raw: Any) -> None:
    button = object_value(ctx, raw, "button")
    kind = string_value(ctx, button.get("type"), "button.type")
    if kind in ("postback", "web_url"):
        string_value(ctx, button.get("title"), "button.title", maximum=20)
        if kind == "postback":
            string_value(ctx, button.get("payload"), "button.payload", maximum=1000)
        else:
            http_url(ctx, button.get("url"), "button.url")
    elif kind not in ("account_link", "account_unlink", "phone_number", "element_share"):
        invalid_param(ctx, "Invalid button type.")


def message_value(ctx: ProviderContext, raw: Any) -> dict[str, Any]:
    message = object_value(ctx, raw, "message")
    content = [k for k in ("text", "attachment", "attachments") if k in message]
    if len(content) != 1:
        invalid_param(ctx, "Message must contain exactly one of text or attachment(s).")
    if "text" in message:
        string_value(ctx, message["text"], "message.text", maximum=2000)
    if "attachment" in message:
        attachment_value(ctx, message["attachment"])
    if "attachments" in message:
        attachments = list_value(ctx, message["attachments"], "attachments", maximum=30)
        if not attachments:
            invalid_param(ctx, "attachments must not be empty.")
        for raw_attachment in attachments:
            attachment = attachment_value(ctx, raw_attachment)
            if attachment["type"] != "image":
                invalid_param(ctx, "Multiple attachments must all be images.")
    if "quick_replies" in message:
        for raw_reply in list_value(ctx, message["quick_replies"], "quick_replies", maximum=13):
            reply = object_value(ctx, raw_reply, "quick_reply")
            kind = string_value(ctx, reply.get("content_type"), "quick_reply.content_type")
            if kind == "text":
                string_value(ctx, reply.get("title"), "quick_reply.title", maximum=20)
                string_value(ctx, reply.get("payload"), "quick_reply.payload", maximum=1000)
            elif kind not in ("user_phone_number", "user_email"):
                invalid_param(ctx, "Invalid quick reply content_type.")
    return message


def profile_updates(ctx: ProviderContext, body: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    updates = {k: body[k] for k in keys if k in body}
    if not updates:
        invalid_param(ctx, "At least one supported Messenger profile field is required.")
    for key, raw in updates.items():
        if key == "get_started":
            value = object_value(ctx, raw, key)
            string_value(ctx, value.get("payload"), "get_started.payload", maximum=1000)
        elif key == "greeting":
            for item in list_value(ctx, raw, key, maximum=100):
                value = object_value(ctx, item, key)
                string_value(ctx, value.get("locale"), "greeting.locale", maximum=32)
                string_value(ctx, value.get("text"), "greeting.text", maximum=160)
        elif key == "domain_whitelist":
            for url in list_value(ctx, raw, key, maximum=50):
                http_url(ctx, url, "domain")
        else:
            for item in list_value(ctx, raw, key, maximum=100):
                value = object_value(ctx, item, key)
                if key == "persistent_menu":
                    string_value(ctx, value.get("locale"), "menu.locale", maximum=32)
                    if (
                        "composer_input_disabled" in value
                        and type(value["composer_input_disabled"]) is not bool
                    ):
                        invalid_param(ctx, "composer_input_disabled must be boolean.")
                    for button in list_value(
                        ctx, value.get("call_to_actions"), "call_to_actions", maximum=20
                    ):
                        button_value(ctx, button)
                else:
                    string_value(ctx, value.get("question"), "ice_breaker.question", maximum=80)
                    string_value(ctx, value.get("payload"), "ice_breaker.payload", maximum=1000)
    return updates
