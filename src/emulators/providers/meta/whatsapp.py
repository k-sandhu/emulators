"""WhatsApp Business Cloud API handlers (mixin for :class:`MetaAPI`).

Send (with the 24-hour customer-service window and template rules), media
upload/metadata/delete, message templates, WABA/phone-number objects, the
business profile and ``subscribed_apps``.
"""

from __future__ import annotations

import re
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.meta import shapes
from emulators.providers.meta.common import (
    DISPLAY_PHONE_DIGITS,
    apply_fields,
    digits_only,
    invalid_param,
    new_wamid,
    not_found,
    numeric_id,
    paged,
    permission_fail,
    require_auth,
    wa_fail,
)
from emulators.providers.meta.webhooks_out import deliver_wa_statuses

WA_MESSAGE_TYPES = {
    "text",
    "image",
    "document",
    "audio",
    "video",
    "sticker",
    "location",
    "contacts",
    "interactive",
    "template",
    "reaction",
}
VERTICALS = {
    "UNDEFINED",
    "OTHER",
    "AUTO",
    "BEAUTY",
    "APPAREL",
    "EDU",
    "ENTERTAIN",
    "EVENT_PLAN",
    "FINANCE",
    "GROCERY",
    "GOVT",
    "HOTEL",
    "HEALTH",
    "NONPROFIT",
    "PROF_SERVICES",
    "RETAIL",
    "SHOPPING",
    "TRAVEL",
    "RESTAURANT",
}
PLACEHOLDER = re.compile(r"\{\{\s*(\d+)\s*\}\}")


def valid_wa_recipient(wa_id: str) -> bool:
    if not wa_id.isdigit() or not (7 <= len(wa_id) <= 15) or wa_id.startswith("0"):
        return False
    if len(set(wa_id)) == 1:
        return False
    # NANP area codes cannot start with 0/1.
    return not (wa_id.startswith("1") and len(wa_id) == 11 and wa_id[1] in "01")


def template_param_counts(template: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for component in template.get("components") or []:
        ctype = str(component.get("type", "")).lower()
        text = str(component.get("text") or "")
        found = [int(n) for n in PLACEHOLDER.findall(text)]
        if found:
            counts[ctype] = max(found)
    return counts


class WhatsAppMixin:
    ctx: ProviderContext
    request: Any
    version: str
    params: dict[str, Any]
    body: dict[str, Any]

    # -- helpers ---------------------------------------------------------------------------------
    def require_whatsapp(self, management: bool = False):
        auth = require_auth(
            self.ctx,
            self.request,
            self.params,
            types={"SYSTEM_USER", "USER"},
            perm_message="Permissions error: WhatsApp Business API calls require a user or "
            "system-user access token with the whatsapp_business_messaging permission.",
            perm_code=200,
        )
        needed = "whatsapp_business_management" if management else "whatsapp_business_messaging"
        if auth.meta_type == "USER" and auth.scopes and needed not in auth.scopes:
            permission_fail(self.ctx, f"Requires {needed} permission.", code=200)
        return auth

    def get_phone(self, phone_id: str) -> dict[str, Any]:
        phone = self.ctx.store.collection("phone_numbers").get(phone_id)
        if phone is None:
            not_found(self.ctx, phone_id)
        assert phone is not None
        return phone

    def get_waba(self, waba_id: str) -> dict[str, Any]:
        waba = self.ctx.store.collection("wabas").get(waba_id)
        if waba is None:
            not_found(self.ctx, waba_id)
        assert waba is not None
        return waba

    def find_or_create_contact(self, wa_id: str, profile_name: str | None = None) -> dict[str, Any]:
        contacts = self.ctx.store.collection("wa_contacts")
        contact = contacts.get(wa_id)
        if contact is not None:
            if profile_name and not contact.get("profile_name"):
                contact["profile_name"] = profile_name
            return contact
        name = profile_name or wa_id
        person_id = None
        for person in self.ctx.world.people:
            for phone in (person.phone_mobile, person.phone_business):
                if phone and digits_only(phone) == wa_id:
                    name = person.display_name
                    person_id = person.id
                    break
            if person_id:
                break
        if person_id is None:
            for lead in self.ctx.world.leads:
                if digits_only(lead.phone) == wa_id:
                    name = f"{lead.first_name} {lead.last_name}"
                    person_id = lead.id
                    break
        contact = {
            "id": wa_id,
            "wa_id": wa_id,
            "profile_name": name,
            "_person_id": person_id,
            "_phone": "+" + wa_id,
            "_last_inbound_ts": None,
            "_conversation_origin": "service",
        }
        contacts.put(contact)
        return contact

    def window_open(self, contact: dict[str, Any]) -> bool:
        last = contact.get("_last_inbound_ts")
        if not last:
            return False
        return (int(self.ctx.clock.now().timestamp()) - int(last)) < 86400

    # -- WABA / phone / profile --------------------------------------------------------------------
    def handle_waba(self, waba_id: str) -> dict[str, Any]:
        self.require_whatsapp(management=True)
        return apply_fields(shapes.waba_shape(self.get_waba(waba_id)), self.params.get("fields"))

    def handle_waba_phone_numbers(self, waba_id: str) -> dict[str, Any]:
        self.require_whatsapp(management=True)
        self.get_waba(waba_id)
        items = [
            apply_fields(shapes.phone_shape(p), self.params.get("fields"))
            for p in self.ctx.store.collection("phone_numbers")
            if p.get("_waba_id") == waba_id
        ]
        return paged(self.ctx, self.request, self.version, f"{waba_id}/phone_numbers", items, self.params)

    def handle_phone(self, phone_id: str) -> dict[str, Any]:
        self.require_whatsapp()
        return apply_fields(shapes.phone_shape(self.get_phone(phone_id)), self.params.get("fields"))

    def handle_profile_get(self, phone_id: str) -> dict[str, Any]:
        self.require_whatsapp()
        phone = self.get_phone(phone_id)
        shaped = apply_fields(shapes.profile_shape(self.ctx, self.request, phone), self.params.get("fields"))
        shaped.setdefault("messaging_product", "whatsapp")
        return {"data": [shaped]}

    def handle_profile_post(self, phone_id: str) -> dict[str, Any]:
        self.require_whatsapp(management=True)
        phone = self.get_phone(phone_id)
        body = self.body
        if body.get("messaging_product") not in (None, "whatsapp"):
            invalid_param(self.ctx, "messaging_product must be 'whatsapp'.")
        fields = (
            "about",
            "address",
            "description",
            "email",
            "websites",
            "vertical",
            "profile_picture_handle",
        )
        updates = {k: body[k] for k in fields if k in body}
        if "vertical" in updates and updates["vertical"] not in VERTICALS:
            invalid_param(self.ctx, "Invalid vertical value.")
        if "websites" in updates and len(updates["websites"] or []) > 2:
            invalid_param(self.ctx, "A business profile can have at most 2 websites.")
        if "email" in updates and updates["email"] and "@" not in str(updates["email"]):
            invalid_param(self.ctx, "Invalid email address.")
        profile = dict(phone.get("_profile") or {})
        profile.update(updates)
        phone["_profile"] = profile
        return {"success": True}

    def handle_subscribed_apps_get(self, waba_id: str) -> dict[str, Any]:
        self.require_whatsapp(management=True)
        self.get_waba(waba_id)
        record = self.ctx.store.collection("subscribed_apps").get(waba_id) or {"apps": []}
        data = [
            {
                "whatsapp_business_api_data": {
                    "category": "Business",
                    "id": app["id"],
                    "link": f"https://www.facebook.com/{app['id']}",
                    "name": app["name"],
                }
            }
            for app in record.get("apps") or []
        ]
        return {"data": data}

    def handle_subscribed_apps_post(self, waba_id: str) -> dict[str, Any]:
        self.require_whatsapp(management=True)
        self.get_waba(waba_id)
        coll = self.ctx.store.collection("subscribed_apps")
        record = coll.get(waba_id) or {"id": waba_id, "apps": []}
        app = {"id": self.ctx.store.meta["app_id"], "name": self.ctx.store.meta["app_name"]}
        if not any(a["id"] == app["id"] for a in record["apps"]):
            record["apps"].append(app)
        coll.put(record)
        return {"success": True}

    def handle_subscribed_apps_delete(self, waba_id: str) -> dict[str, Any]:
        self.require_whatsapp(management=True)
        self.get_waba(waba_id)
        self.ctx.store.collection("subscribed_apps").put({"id": waba_id, "apps": []})
        return {"success": True}

    def handle_register(self, phone_id: str, deregister: bool) -> dict[str, Any]:
        self.require_whatsapp(management=True)
        self.get_phone(phone_id)
        if self.body.get("messaging_product") not in (None, "whatsapp"):
            invalid_param(self.ctx, "messaging_product must be 'whatsapp'.")
        if not deregister and self.body.get("pin") is not None:
            pin = str(self.body["pin"])
            if not (pin.isdigit() and len(pin) == 6):
                invalid_param(self.ctx, "pin must be a 6-digit number.")
        return {"success": True}

    # -- templates ---------------------------------------------------------------------------------
    def find_template(self, name: str, language: str | None = None) -> dict[str, Any] | None:
        for template in self.ctx.store.collection("templates"):
            if template["name"] == name and (language is None or template["language"] == language):
                return template
        return None

    def handle_templates_get(self, waba_id: str) -> dict[str, Any]:
        self.require_whatsapp(management=True)
        self.get_waba(waba_id)
        items = []
        for template in self.ctx.store.collection("templates"):
            if template.get("_waba_id") != waba_id:
                continue
            if self.params.get("name") and template["name"] != self.params["name"]:
                continue
            if self.params.get("language") and template["language"] != self.params["language"]:
                continue
            if self.params.get("category") and template["category"] != self.params["category"]:
                continue
            needle = self.params.get("name_or_content")
            if (
                needle
                and str(needle).lower() not in template["name"].lower()
                and str(needle).lower() not in str(template["components"]).lower()
            ):
                continue
            items.append(apply_fields(shapes.template_shape(template), self.params.get("fields")))
        items.sort(key=lambda t: t.get("name", ""))
        return paged(self.ctx, self.request, self.version, f"{waba_id}/message_templates", items, self.params)

    def handle_templates_post(self, waba_id: str) -> dict[str, Any]:
        self.require_whatsapp(management=True)
        self.get_waba(waba_id)
        body = self.body
        name = body.get("name")
        language = body.get("language")
        category = body.get("category")
        components = body.get("components")
        if not name or not re.fullmatch(r"[a-z0-9_]+", str(name)):
            invalid_param(self.ctx, "Template name must be lowercase letters, digits and underscores.")
        if not language:
            invalid_param(self.ctx, "The parameter language is required.")
        if category not in ("MARKETING", "UTILITY", "AUTHENTICATION"):
            invalid_param(self.ctx, "category must be MARKETING, UTILITY or AUTHENTICATION.")
        if not isinstance(components, list) or not components:
            invalid_param(self.ctx, "The parameter components is required.")
        if not any(str(c.get("type", "")).upper() == "BODY" for c in components if isinstance(c, dict)):
            invalid_param(self.ctx, "A template must have a BODY component.")
        if self.find_template(str(name), str(language)) is not None:
            invalid_param(self.ctx, f"A template named {name} ({language}) already exists.")
        status = "APPROVED" if (self.ctx.tokens.lenient_clients and category == "UTILITY") else "PENDING"
        record = {
            "id": numeric_id(
                "template", waba_id, name, language, self.ctx.ids.sequence("template"), digits=16
            ),
            "name": str(name),
            "language": str(language),
            "category": category,
            "status": status,
            "components": components,
            "quality_score": {"score": "GREEN" if status == "APPROVED" else "UNKNOWN"},
            "_waba_id": waba_id,
        }
        if body.get("allow_category_change") is not None:
            record["allow_category_change"] = bool(body["allow_category_change"])
        self.ctx.store.collection("templates").put(record)
        return {"id": record["id"], "status": status, "category": category}

    def handle_templates_delete(self, waba_id: str) -> dict[str, Any]:
        self.require_whatsapp(management=True)
        self.get_waba(waba_id)
        name = self.params.get("name") or self.body.get("name")
        if not name:
            invalid_param(self.ctx, "The parameter name is required to delete templates.")
        coll = self.ctx.store.collection("templates")
        deleted = 0
        for template in list(coll):
            if template["name"] == name and template.get("_waba_id") == waba_id:
                coll.delete(template["id"])
                deleted += 1
        if deleted == 0:
            invalid_param(self.ctx, f"No template named {name} exists.")
        return {"success": True}

    # -- media -------------------------------------------------------------------------------------
    async def handle_media_upload(self, phone_id: str) -> dict[str, Any]:
        self.require_whatsapp()
        self.get_phone(phone_id)
        form = await self.request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            invalid_param(self.ctx, "The parameter file is required (multipart form upload).")
        content: bytes = await upload.read()  # type: ignore[union-attr]
        mime = str(form.get("type") or getattr(upload, "content_type", "") or "application/octet-stream")
        if str(form.get("messaging_product") or "whatsapp") != "whatsapp":
            invalid_param(self.ctx, "messaging_product must be 'whatsapp'.")
        blob_key = self.ctx.store.blobs.put(content)
        media_id = numeric_id("media", blob_key, self.ctx.ids.sequence("media"), digits=16)
        self.ctx.store.collection("media").put(
            {
                "id": media_id,
                "messaging_product": "whatsapp",
                "mime_type": mime,
                "sha256": blob_key,
                "file_size": len(content),
                "_blob_key": blob_key,
                "_filename": getattr(upload, "filename", None) or "upload",
                "_phone_number_id": phone_id,
            }
        )
        return {"id": media_id}

    def get_media(self, media_id: str) -> dict[str, Any]:
        record = self.ctx.store.collection("media").get(media_id)
        if record is None:
            invalid_param(
                self.ctx,
                f"Object with ID '{media_id}' does not exist, cannot be loaded due to missing "
                "permissions, or does not support this operation.",
                subcode=33,
            )
        assert record is not None
        return record

    def handle_media_get(self, media_id: str) -> dict[str, Any]:
        self.require_whatsapp()
        record = self.get_media(media_id)
        return apply_fields(
            shapes.media_shape(self.ctx, self.request, record, self.version), self.params.get("fields")
        )

    def handle_media_delete(self, media_id: str) -> dict[str, Any]:
        self.require_whatsapp()
        self.get_media(media_id)
        phone_scope = self.params.get("phone_number_id")
        if phone_scope and str(phone_scope) not in self.ctx.store.collection("phone_numbers"):
            not_found(self.ctx, str(phone_scope))
        self.ctx.store.collection("media").delete(media_id)
        return {"success": True}

    # -- sending -------------------------------------------------------------------------------------
    def _validate_media_ref(self, payload: dict[str, Any], kind: str) -> None:
        media_id = payload.get("id")
        if media_id:
            if self.ctx.store.collection("media").get(str(media_id)) is None:
                invalid_param(self.ctx, f"Media object with ID '{media_id}' does not exist.", subcode=33)
        elif not payload.get("link"):
            invalid_param(self.ctx, f"The parameter {kind} must contain either 'id' or 'link'.")

    async def handle_wa_send(self, phone_id: str) -> dict[str, Any]:
        self.require_whatsapp()
        self.get_phone(phone_id)
        body = self.body
        if body.get("messaging_product") != "whatsapp":
            invalid_param(self.ctx, "messaging_product must be 'whatsapp'.")
        # Mark-as-read shares this endpoint.
        if body.get("status") == "read":
            message_id = body.get("message_id")
            if not message_id:
                invalid_param(self.ctx, "The parameter message_id is required.")
            record = self.ctx.store.collection("wa_messages").get(str(message_id))
            if record is None:
                invalid_param(self.ctx, f"Message with ID '{message_id}' does not exist.", subcode=33)
            record["status"] = "read"
            return {"success": True}
        recipient_type = body.get("recipient_type", "individual")
        if recipient_type != "individual":
            wa_fail(
                self.ctx,
                131051,
                "Unsupported message type",
                f"recipient_type '{recipient_type}' is not supported; only 'individual' is.",
            )
        to_raw = str(body.get("to") or "")
        wa_id = digits_only(to_raw)
        if not to_raw or not valid_wa_recipient(wa_id):
            wa_fail(
                self.ctx,
                131026,
                "Message undeliverable",
                f"Receiver {to_raw or '(empty)'} is not a valid WhatsApp user.",
            )
        msg_type = body.get("type") or next((t for t in WA_MESSAGE_TYPES if t in body), None)
        if msg_type not in WA_MESSAGE_TYPES:
            wa_fail(
                self.ctx,
                131051,
                "Unsupported message type",
                f"Message type '{msg_type}' is not supported.",
            )
        assert msg_type is not None
        payload = body.get(msg_type)
        origin = "service"
        billable = False
        template_record: dict[str, Any] | None = None
        if msg_type == "template":
            if not isinstance(payload, dict) or not payload.get("name"):
                invalid_param(self.ctx, "The parameter template['name'] is required.")
            language = (payload.get("language") or {}).get("code") or payload.get("language")
            template_record = self.find_template(str(payload["name"]), str(language) if language else None)
            if template_record is None or template_record["status"] != "APPROVED":
                wa_fail(
                    self.ctx,
                    132001,
                    "Template does not exist in the translation",
                    f"Template {payload['name']} ({language or 'en_US'}) does not exist or is not approved.",
                )
            assert template_record is not None
            expected = template_param_counts(template_record)
            provided: dict[str, int] = {}
            for component in payload.get("components") or []:
                if isinstance(component, dict):
                    provided[str(component.get("type", "")).lower()] = len(component.get("parameters") or [])
            for ctype, count in expected.items():
                if provided.get(ctype, 0) != count:
                    wa_fail(
                        self.ctx,
                        132000,
                        "Number of parameters does not match the expected number of params",
                        f"Template {template_record['name']} expects {count} {ctype} parameter(s) "
                        f"but received {provided.get(ctype, 0)}.",
                    )
            for ctype, count in provided.items():
                if ctype not in expected and count:
                    wa_fail(
                        self.ctx,
                        132000,
                        "Number of parameters does not match the expected number of params",
                        f"Template {template_record['name']} has no {ctype} parameters but received {count}.",
                    )
            origin = template_record["category"].lower()
            billable = True
        else:
            contact = self.find_or_create_contact(wa_id)
            if not self.window_open(contact):
                wa_fail(
                    self.ctx,
                    131047,
                    "Re-engagement message",
                    "More than 24 hours have passed since the recipient last replied. "
                    "Use a template message to re-engage.",
                )
            # Per-type required payloads.
            if msg_type == "text":
                if not isinstance(payload, dict) or not payload.get("body"):
                    invalid_param(self.ctx, "The parameter text['body'] is required.")
            elif msg_type in ("image", "document", "audio", "video", "sticker"):
                if not isinstance(payload, dict):
                    invalid_param(self.ctx, f"The parameter {msg_type} is required.")
                self._validate_media_ref(payload, msg_type)  # type: ignore[arg-type]
            elif msg_type == "location":
                if (
                    not isinstance(payload, dict)
                    or payload.get("latitude") is None
                    or payload.get("longitude") is None
                ):
                    invalid_param(self.ctx, "location must contain latitude and longitude.")
            elif msg_type == "contacts":
                if not isinstance(payload, list) or not payload:
                    invalid_param(self.ctx, "The parameter contacts must be a non-empty list.")
            elif msg_type == "interactive":
                if not isinstance(payload, dict) or not payload.get("type"):
                    invalid_param(self.ctx, "The parameter interactive['type'] is required.")
            elif msg_type == "reaction" and (not isinstance(payload, dict) or not payload.get("message_id")):
                invalid_param(self.ctx, "The parameter reaction['message_id'] is required.")
        contact = self.find_or_create_contact(wa_id)
        context = body.get("context")
        if (
            context
            and context.get("message_id")
            and self.ctx.store.collection("wa_messages").get(str(context["message_id"])) is None
        ):
            invalid_param(self.ctx, "context.message_id does not exist.", subcode=33)
        message_id = new_wamid(self.ctx)
        record: dict[str, Any] = {
            "id": message_id,
            "direction": "outbound",
            "from": DISPLAY_PHONE_DIGITS,
            "recipient_id": wa_id,
            "to": wa_id,
            "timestamp": str(int(self.ctx.clock.now().timestamp())),
            "type": msg_type,
            "status": "accepted",
            "_person_id": contact.get("_person_id"),
            "_phone_number_id": phone_id,
            "_origin": origin,
            "_billable": billable,
        }
        if isinstance(payload, (dict, list)):
            record[msg_type] = payload
        if context:
            record["context"] = context
        if template_record is not None:
            record["_template"] = template_record["name"]
            contact["_conversation_origin"] = origin
        self.ctx.store.collection("wa_messages").put(record)
        await deliver_wa_statuses(self.ctx, record)
        return {
            "messaging_product": "whatsapp",
            "contacts": [{"input": to_raw, "wa_id": wa_id}],
            "messages": [{"id": message_id, "message_status": "accepted"}],
        }
