"""Accounting resources: Xero envelopes, mutations, files, and audit history."""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext, url_for_host
from emulators.world.documents import make_pdf

from .common import (
    BASE,
    ID_FIELDS,
    READ_ONLY,
    access,
    envelope,
    find,
    history,
    instant,
    missing,
    money,
    msdate,
    public,
    rows,
    stamp,
    validation,
)
from .hooks import notify
from .ledger import contact_balances
from .query import select
from .reports import report
from .transactions import mutate, settle


def known(resource: str) -> None:
    if resource not in ID_FIELDS:
        missing()


async def payload(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        validation("A JSON request body is required.")
    if not isinstance(data, dict):
        validation("The request body must be an object.")
    return data


async def write(
    ctx: ProviderContext, request: Request, resource: str, identifier: str | None = None
) -> dict[str, Any]:
    known(resource)
    _, tenant = access(ctx, request, resource, write=True)
    if resource in READ_ONLY:
        raise EmulatorError(405, {"Message": "Method not allowed"})
    data = await payload(request)
    records = data.get(resource, [data])
    if not isinstance(records, list) or not records:
        validation(f"{resource} must be a non-empty array.")
    if identifier and len(records) != 1:
        validation("Only one element can be updated through an individual resource URL.")
    key = request.headers.get("idempotency-key")
    cache_id = f"{tenant}/{request.method}/{request.url.path}/{key}"
    digest = hashlib.sha256(await request.body()).hexdigest()
    if key:
        if len(key) > 128:
            validation("Idempotency-Key must not exceed 128 characters.")
        cached = ctx.store.collection("_idempotency").get(cache_id)
        if cached:
            if cached["digest"] != digest:
                validation("Idempotency-Key has already been used with a different request.")
            return cached["response"]
    summarize = request.query_params.get("summarizeErrors", "true").lower() != "false"
    snapshot = ctx.store.snapshot()
    results, events = [], []
    for value in records:
        before = ctx.store.snapshot() if not summarize else snapshot
        try:
            if (
                request.method == "PUT"
                and isinstance(value, dict)
                and any(
                    r[ID_FIELDS[resource]] == value.get(ID_FIELDS[resource])
                    for r in rows(ctx, resource, tenant)
                )
            ):
                validation("PUT creates new resources; use POST to update.", value)
            result, created = mutate(ctx, tenant, resource, value, identifier)
            result.pop("_pdf", None)
            results.append(copy.deepcopy(result))
            events.append((copy.deepcopy(result), created))
        except EmulatorError as exc:
            ctx.store.restore(before)
            if summarize or exc.status != 400:
                ctx.store.restore(snapshot)
                raise
            element = exc.body["Elements"][0]
            results.append(
                {
                    **(public(value) if isinstance(value, dict) else {}),
                    "HasValidationErrors": True,
                    "ValidationErrors": element["ValidationErrors"],
                }
            )
    response = envelope(ctx, resource, results)
    if key:
        ctx.store.collection("_idempotency").put({"id": cache_id, "digest": digest, "response": response})
    for record, created in events:
        await notify(ctx, request, tenant, resource, record, created)
    return response


def build(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get(BASE + "/Reports/{name}")
    async def get_report(request: Request, name: str) -> dict[str, Any]:
        _, tenant = access(ctx, request, "Reports/" + name)
        return report(ctx, request, tenant, name)

    @app.get(BASE + "/Invoices/{identifier}/OnlineInvoice")
    async def online(request: Request, identifier: str) -> dict[str, Any]:
        _, tenant = access(ctx, request, "Invoices")
        invoice = find(ctx, "Invoices", tenant, identifier)
        if invoice["Type"] != "ACCREC" or invoice["Status"] not in {"AUTHORISED", "PAID"}:
            validation("Online invoices are available only for authorised sales invoices.")
        return envelope(
            ctx,
            "OnlineInvoices",
            [
                {
                    "OnlineInvoiceUrl": url_for_host(
                        request,
                        ctx,
                        "go.xero.com",
                        "/AccountsReceivable/View.aspx?InvoiceID=" + invoice["InvoiceID"],
                    )
                }
            ],
        )

    @app.post(BASE + "/Invoices/{identifier}/Email")
    async def email(request: Request, identifier: str) -> Response:
        _, tenant = access(ctx, request, "Invoices", write=True)
        invoice = find(ctx, "Invoices", tenant, identifier)
        contact = find(ctx, "Contacts", tenant, invoice["Contact"]["ContactID"])
        if (
            invoice["Type"] != "ACCREC"
            or invoice["Status"] not in {"AUTHORISED", "PAID"}
            or not contact.get("EmailAddress")
        ):
            validation("Only approved sales invoices for contacts with an email address may be emailed.")
        invoice["SentToContact"] = True
        stamp(ctx, invoice)
        history(
            ctx,
            tenant,
            "Invoices",
            invoice["InvoiceID"],
            "Invoice emailed to " + contact["EmailAddress"],
            changes="Email Sent",
        )
        await notify(ctx, request, tenant, "Invoices", invoice, False)
        return Response(status_code=204)

    @app.get(BASE + "/Invoices/{identifier}/Attachments")
    async def attachments(request: Request, identifier: str) -> dict[str, Any]:
        _, tenant = access(ctx, request, "Attachments")
        invoice = find(ctx, "Invoices", tenant, identifier)
        attachments = ctx.store.collection("_attachments").where(_tenant=tenant, _parent=invoice["InvoiceID"])
        return envelope(
            ctx,
            "Attachments",
            [
                {
                    **public(a),
                    "Url": url_for_host(
                        request,
                        ctx,
                        "api.xero.com",
                        f"{BASE}/Invoices/{invoice['InvoiceID']}/Attachments/{a['AttachmentID']}",
                    ),
                }
                for a in attachments
            ],
        )

    @app.api_route(BASE + "/Invoices/{identifier}/Attachments/{filename}", methods=["GET", "PUT", "POST"])
    async def attachment(request: Request, identifier: str, filename: str) -> Response:
        write_file = request.method != "GET"
        _, tenant = access(ctx, request, "Attachments", write=write_file)
        # Files scope alone never grants access to an unrelated ledger organisation.
        invoice = find(ctx, "Invoices", tenant, identifier)
        collection = ctx.store.collection("_attachments")
        old = next(
            (
                a
                for a in collection
                if a["_tenant"] == tenant
                and a["_parent"] == invoice["InvoiceID"]
                and filename in {a["FileName"], a["AttachmentID"]}
            ),
            None,
        )
        if not write_file:
            if not old:
                missing()
            return Response(ctx.store.blobs.get(old["_blob"]), media_type=old["MimeType"])
        content = await request.body()
        if len(content) > 25 * 1024 * 1024:
            validation("Attachments must not exceed 25MB.")
        if not old and len(collection.where(_tenant=tenant, _parent=invoice["InvoiceID"])) >= 10:
            validation("An invoice may have at most ten attachments.")
        if not filename or "/" in filename or "\\" in filename:
            validation("Invalid filename.")
        attachment_id = old["AttachmentID"] if old else ctx.ids.guid()
        record = {
            "AttachmentID": attachment_id,
            "FileName": old["FileName"] if old else filename,
            "MimeType": request.headers.get("content-type", "application/octet-stream"),
            "ContentLength": len(content),
            "IncludeOnline": request.query_params.get("IncludeOnline", "false").lower() == "true",
            "_tenant": tenant,
            "_parent": invoice["InvoiceID"],
            "_blob": ctx.store.blobs.put(content),
        }
        collection.put_as(tenant + "/" + invoice["InvoiceID"] + "/" + attachment_id, record)
        invoice["HasAttachments"] = True
        stamp(ctx, invoice)
        history(ctx, tenant, "Invoices", invoice["InvoiceID"], "Attachment uploaded: " + record["FileName"])
        await notify(ctx, request, tenant, "Invoices", invoice, False)
        return JSONResponse(envelope(ctx, "Attachments", [record]))

    @app.api_route(BASE + "/{resource}/{identifier}/History", methods=["GET", "PUT"])
    async def notes(request: Request, resource: str, identifier: str) -> dict[str, Any]:
        if resource not in {"Invoices", "Contacts", "Payments", "CreditNotes", "BankTransactions", "Quotes"}:
            missing()
        _, tenant = access(ctx, request, resource, write=request.method != "GET")
        record = find(ctx, resource, tenant, identifier)
        identifier = record[ID_FIELDS[resource]]
        if request.method == "PUT":
            data = await payload(request)
            notes = data.get("HistoryRecords", [])
            if (
                not isinstance(notes, list)
                or not notes
                or any(not isinstance(n, dict) or not n.get("Details") for n in notes)
            ):
                validation("HistoryRecords must contain a Details value.")
            for note in notes:
                history(ctx, tenant, resource, identifier, note["Details"], changes="Note")
        records = ctx.store.collection("_history").where(
            _tenant=tenant, _resource=resource, _parent=identifier
        )
        return envelope(ctx, "HistoryRecords", [{k: v for k, v in r.items() if k != "id"} for r in records])

    @app.api_route(BASE + "/ContactGroups/{identifier}/Contacts", methods=["PUT"])
    async def group_contacts(request: Request, identifier: str) -> dict[str, Any]:
        _, tenant = access(ctx, request, "ContactGroups", write=True)
        group = find(ctx, "ContactGroups", tenant, identifier)
        data = await payload(request)
        members = data.get("Contacts", [])
        if not isinstance(members, list) or not members:
            validation("Contacts is required.")
        resolved = [find(ctx, "Contacts", tenant, c.get("ContactID", "")) for c in members]
        for contact in resolved:
            if not any(c["ContactID"] == contact["ContactID"] for c in group["Contacts"]):
                group["Contacts"].append({"ContactID": contact["ContactID"]})
                contact.setdefault("ContactGroups", []).append(
                    {k: group[k] for k in ["ContactGroupID", "Name", "Status"]}
                )
        return envelope(ctx, "Contacts", resolved)

    @app.delete(BASE + "/ContactGroups/{identifier}/Contacts/{contact_id}")
    async def remove_group_contact(request: Request, identifier: str, contact_id: str) -> Response:
        _, tenant = access(ctx, request, "ContactGroups", write=True)
        group = find(ctx, "ContactGroups", tenant, identifier)
        contact = find(ctx, "Contacts", tenant, contact_id)
        group["Contacts"] = [c for c in group["Contacts"] if c["ContactID"] != contact["ContactID"]]
        contact["ContactGroups"] = [
            g for g in contact.get("ContactGroups", []) if g["ContactGroupID"] != group["ContactGroupID"]
        ]
        return Response(status_code=204)

    @app.api_route(BASE + "/TrackingCategories/{identifier}/Options", methods=["PUT"])
    async def add_option(request: Request, identifier: str) -> dict[str, Any]:
        _, tenant = access(ctx, request, "TrackingCategories", write=True)
        category = find(ctx, "TrackingCategories", tenant, identifier)
        data = await payload(request)
        if not isinstance(data.get("Name"), str) or not data["Name"].strip():
            validation("Name is required.")
        if any(o["Name"].casefold() == data["Name"].casefold() for o in category["Options"]):
            validation("Option Name must be unique.")
        option = {"TrackingOptionID": ctx.ids.guid(), "Name": data["Name"], "Status": "ACTIVE"}
        category["Options"].append(option)
        return envelope(ctx, "Options", [option])

    @app.put(BASE + "/CreditNotes/{identifier}/Allocations")
    async def allocate(request: Request, identifier: str) -> dict[str, Any]:
        _, tenant = access(ctx, request, "CreditNotes", write=True)
        credit = find(ctx, "CreditNotes", tenant, identifier)
        data = await payload(request)
        allocations = data.get("Allocations", [])
        if credit["Status"] != "AUTHORISED" or not isinstance(allocations, list) or not allocations:
            validation("Allocations require an authorised credit note.")
        snapshot = ctx.store.snapshot()
        result = []
        try:
            for value in allocations:
                if not isinstance(value, dict) or not isinstance(value.get("Invoice"), dict):
                    validation("Each allocation requires an Invoice object.")
                invoice = find(ctx, "Invoices", tenant, value.get("Invoice", {}).get("InvoiceID", ""))
                amount = money(value.get("Amount", 0))
                if (
                    invoice["Status"] != "AUTHORISED"
                    or credit["Contact"]["ContactID"] != invoice["Contact"]["ContactID"]
                    or credit["Type"] != invoice["Type"] + "CREDIT"
                ):
                    validation("The allocation invoice must be authorised and match contact and type.")
                if amount <= 0 or amount > credit["RemainingCredit"] or amount > invoice["AmountDue"]:
                    validation("Allocation exceeds available credit or invoice amount due.")
                allocation_date = instant(value.get("Date", ctx.clock.now().date().isoformat()))
                if allocation_date < max(instant(invoice["Date"]), instant(credit["Date"])):
                    validation("Allocation date precedes the invoice or credit note.")
                record = {
                    "AllocationID": ctx.ids.guid(),
                    "Invoice": {"InvoiceID": invoice["InvoiceID"]},
                    "Amount": amount,
                    "Date": msdate(allocation_date),
                }
                credit["Allocations"].append(record)
                credit["RemainingCredit"] = money(credit["RemainingCredit"] - amount)
                settle(ctx, tenant, invoice)
                result.append(record)
            if credit["RemainingCredit"] == 0:
                credit["Status"] = "PAID"
                credit["FullyPaidOnDate"] = max((a["Date"] for a in credit["Allocations"]), key=instant)
            stamp(ctx, credit)
        except EmulatorError:
            ctx.store.restore(snapshot)
            raise
        except (ValueError, TypeError):
            ctx.store.restore(snapshot)
            validation("The allocation date is invalid.")
        return envelope(ctx, "Allocations", result)

    @app.delete(BASE + "/CreditNotes/{identifier}/Allocations/{allocation_id}")
    async def unallocate(request: Request, identifier: str, allocation_id: str) -> Response:
        _, tenant = access(ctx, request, "CreditNotes", write=True)
        credit = find(ctx, "CreditNotes", tenant, identifier)
        allocation = next((a for a in credit["Allocations"] if a["AllocationID"] == allocation_id), None)
        if not allocation:
            missing()
        credit["Allocations"].remove(allocation)
        credit["RemainingCredit"] = money(credit["RemainingCredit"] + allocation["Amount"])
        credit["Status"] = "AUTHORISED"
        credit.pop("FullyPaidOnDate", None)
        stamp(ctx, credit)
        settle(ctx, tenant, find(ctx, "Invoices", tenant, allocation["Invoice"]["InvoiceID"]))
        return Response(status_code=204)

    @app.get(BASE + "/{resource}")
    async def listing(request: Request, resource: str) -> dict[str, Any]:
        known(resource)
        _, tenant = access(ctx, request, resource)
        records = rows(ctx, resource, tenant)
        if resource == "Contacts":
            records = [{**r, "Balances": contact_balances(ctx, tenant, r)} for r in records]
        selected, extra = select(request, resource, records)
        return envelope(ctx, "Organisations" if resource == "Organisation" else resource, selected, **extra)

    @app.get(BASE + "/{resource}/{identifier}")
    async def individual(request: Request, resource: str, identifier: str) -> Response:
        known(resource)
        _, tenant = access(ctx, request, resource)
        record = find(ctx, resource, tenant, identifier)
        if resource == "Invoices" and "application/pdf" in request.headers.get("accept", ""):
            if not record.get("_pdf"):
                paragraphs = [
                    "Halcyon Advisory Group",
                    record["Contact"]["Name"],
                    "Date: " + record["DateString"][:10],
                    "Due: " + record["DueDateString"][:10],
                    *[
                        line["Description"]
                        + f": {line['Quantity']:g} x {line['UnitAmount']:.2f} = {line['LineAmount']:.2f}"
                        for line in record["LineItems"]
                    ],
                    f"Subtotal: {record['SubTotal']:.2f}",
                    f"Tax: {record['TotalTax']:.2f}",
                    f"Total: {record['Total']:.2f} USD",
                    f"Amount due: {record['AmountDue']:.2f} USD",
                ]
                record["_pdf"] = ctx.store.blobs.put(
                    make_pdf(record["InvoiceNumber"], paragraphs, created=instant(record["Date"]))
                )
            return Response(ctx.store.blobs.get(record["_pdf"]), media_type="application/pdf")
        if resource == "Contacts":
            record = {**record, "Balances": contact_balances(ctx, tenant, record)}
        return JSONResponse(envelope(ctx, resource, [record]))

    @app.api_route(BASE + "/{resource}", methods=["PUT", "POST"])
    async def create(request: Request, resource: str) -> dict[str, Any]:
        return await write(ctx, request, resource)

    @app.post(BASE + "/{resource}/{identifier}")
    async def update(request: Request, resource: str, identifier: str) -> dict[str, Any]:
        return await write(ctx, request, resource, identifier)

    @app.delete(BASE + "/{resource}/{identifier}")
    async def delete(request: Request, resource: str, identifier: str) -> Response:
        if resource not in {"Accounts", "Items", "ContactGroups", "TrackingCategories"}:
            raise EmulatorError(405, {"Message": "Method not allowed"})
        _, tenant = access(ctx, request, resource, write=True)
        record = find(ctx, resource, tenant, identifier)
        if resource == "Accounts":
            from .ledger import postings

            if record.get("SystemAccount") or any(
                p.code == record["Code"] and p.amount for p in postings(ctx, tenant)
            ):
                validation("An account with transactions cannot be deleted; archive it instead.")
            record["Status"] = "DELETED"
        elif resource == "Items":
            ctx.store.collection(resource).delete(tenant + "/" + record[ID_FIELDS[resource]])
            return Response(status_code=204)
        else:
            record["Status"] = "DELETED"
        return JSONResponse(envelope(ctx, resource, [record]))
