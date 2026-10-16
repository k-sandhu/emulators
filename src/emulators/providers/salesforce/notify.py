"""Outbound messages: SOAP notifications posted to a configured endpoint when records change.

Configured through the ``configure_outbound_message`` action (the emulator's stand-in for a
Workflow Outbound Message). Streaming API / Change Data Capture (CometD) are not emulated.
"""

from __future__ import annotations

from typing import Any
from xml.sax.saxutils import escape

from emulators.core.ids import salesforce_id
from emulators.core.provider import ProviderContext, ctx_url_for_host
from emulators.providers.salesforce.common import INSTANCE_HOST
from emulators.providers.salesforce.org import Org
from emulators.providers.salesforce.schema import SCHEMA

OUTBOUND = "_sf_outbound"
EVENT_KINDS = ("create", "update", "delete", "undelete")
NOTIFICATION_NS = "http://soap.sforce.com/2005/09/outbound"


def _xml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return escape(str(value))


def notification_xml(
    org: Org, cfg: dict[str, Any], records: list[tuple[str, dict[str, Any]]], version: str = "62.0"
) -> str:
    oid = org.org_id18
    items = []
    for notification_id, rec in records:
        obj = SCHEMA.objects[cfg["object"]]
        wanted = cfg.get("fields") or [f.name for f in obj.fields if f.type != "address"]
        cells = "".join(
            f"<sf:{name}>{_xml_value(rec[name])}</sf:{name}>"
            for name in dict.fromkeys(["Id", *wanted])
            if rec.get(name) is not None
        )
        items.append(
            f"<Notification><Id>{notification_id}</Id>"
            f'<sObject xsi:type="sf:{obj.name}" xmlns:sf="urn:sobject.enterprise.soap.sforce.com">{cells}</sObject>'
            "</Notification>"
        )
    base = ctx_url_for_host(org.ctx, INSTANCE_HOST, "").rstrip("/")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<soapenv:Body><notifications xmlns="{NOTIFICATION_NS}">'
        f"<OrganizationId>{oid}</OrganizationId><ActionId>{cfg['id']}</ActionId>"
        '<SessionId xsi:nil="true"/>'
        f"<EnterpriseUrl>{base}/services/Soap/c/{version}/{oid}</EnterpriseUrl>"
        f"<PartnerUrl>{base}/services/Soap/u/{version}/{oid}</PartnerUrl>"
        f"{''.join(items)}</notifications></soapenv:Body></soapenv:Envelope>"
    )


class Notifier:
    def __init__(self, ctx: ProviderContext, org: Org) -> None:
        self.ctx = ctx
        self.org = org

    def configure(
        self, obj_name: str, url: str, events: list[str] | None, fields: list[str] | None, name: str | None
    ) -> dict[str, Any]:
        obj = SCHEMA.get(obj_name) if isinstance(obj_name, str) else None
        if obj is None:
            raise ValueError(f"unknown sObject '{obj_name}'")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")) or len(url) > 2000:
            raise ValueError("'url' must be an http(s) endpoint")
        if events is not None and (
            not isinstance(events, list) or not events or not all(isinstance(e, str) for e in events)
        ):
            raise ValueError("'events' must be a non-empty list of create, update, delete or undelete")
        for event in events or []:
            if event.lower() not in EVENT_KINDS:
                raise ValueError(f"unknown event '{event}' (expected create, update, delete or undelete)")
        if fields is not None and not isinstance(fields, list):
            raise ValueError("'fields' must be a list of field names")
        canonical: list[str] = []
        for field_name in fields or []:
            field = obj.field(field_name) if isinstance(field_name, str) else None
            if field is None:
                raise ValueError(f"unknown field '{field_name}' on {obj.name}")
            canonical.append(field.name)
        if name is not None and not isinstance(name, str):
            raise ValueError("'name' must be a string")
        seq = self.org.meta["seq"]
        seq["04k"] = seq.get("04k", 0) + 1
        cfg_id = salesforce_id("04k", seq["04k"])
        cfg = {
            "id": cfg_id,
            "name": name or f"{obj.name}_Outbound",
            "object": obj.name,
            "url": url,
            "events": [e.lower() for e in (events or ["create", "update"])],
            "fields": canonical,
            "active": True,
        }
        self.org.coll(OUTBOUND).put_as(cfg_id, cfg)
        return cfg

    async def flush(self, events: list[tuple[str, str, str]]) -> None:
        """Send one SOAP message per (config, operation) for the committed changes of a request."""
        configs = [c for c in self.org.coll(OUTBOUND) if c.get("active")]
        if not configs or not events:
            return
        for cfg in configs:
            for op in cfg["events"]:
                seen: set[str] = set()
                batch: list[tuple[str, dict[str, Any]]] = []
                for kind, obj_name, rid in events:
                    if kind != op or obj_name != cfg["object"] or rid in seen:
                        continue
                    rec = self.org.coll(obj_name).get(rid)
                    if rec is None:
                        continue
                    seen.add(rid)
                    seq = self.org.meta["seq"]
                    seq["04l"] = seq.get("04l", 0) + 1
                    batch.append((salesforce_id("04l", seq["04l"]), rec))
                if not batch:
                    continue
                for start in range(0, len(batch), 100):
                    chunk = batch[start : start + 100]
                    body = notification_xml(self.org, cfg, chunk).encode()
                    await self.ctx.webhooks.deliver(
                        cfg["url"],
                        body,
                        content_type="text/xml; charset=UTF-8",
                        headers={"SOAPAction": '""'},
                        event_type=f"outbound.{cfg['object']}.{op}",
                        metadata={"config": cfg["id"], "records": [r["Id"] for _, r in chunk]},
                    )
