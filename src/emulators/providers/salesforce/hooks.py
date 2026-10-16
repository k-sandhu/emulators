"""Per-object business logic that runs around saves: derived fields, defaults, validation rules.

These are the "triggers and formula fields" of the emulated org: Opportunity stage semantics,
Case numbering/closing, Task/Event date maths, Contact/Lead names, line-item roll-ups and the two
org validation rules (Opportunity amount, Engagement dates).
"""

from __future__ import annotations

import hashlib
import mimetypes
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from emulators.providers.salesforce.common import SfError, fmt_dt, parse_dt
from emulators.providers.salesforce.opp_logic import CATEGORY_BY_NAME, NAME_BY_CATEGORY, stage_info
from emulators.providers.salesforce.org import Record
from emulators.providers.salesforce.schema import SCHEMA
from emulators.providers.salesforce.seedlib import blank

if TYPE_CHECKING:
    from emulators.providers.salesforce.dml import Dml
    from emulators.providers.salesforce.schema import SObject


def _name(rec: Record) -> str:
    return " ".join(p for p in (rec.get("FirstName"), rec.get("LastName")) if p)


def before_save(dml: Dml, obj: SObject, rec: Record, old: Record | None, provided: set[str]) -> None:
    handler = _BEFORE.get(obj.name)
    if handler:
        handler(dml, rec, old, provided)


def validate(dml: Dml, obj: SObject, rec: Record, old: Record | None) -> None:
    """Org validation rules (``FIELD_CUSTOM_VALIDATION_EXCEPTION``)."""
    if obj.name == "Opportunity" and rec.get("Amount") is not None and rec["Amount"] < 0:
        raise SfError(
            400, "FIELD_CUSTOM_VALIDATION_EXCEPTION", "Opportunity amount cannot be negative.", ["Amount"]
        )
    if obj.name == "Engagement__c":
        start, end = rec.get("Start_Date__c"), rec.get("End_Date__c")
        if start and end and end < start:
            raise SfError(
                400,
                "FIELD_CUSTOM_VALIDATION_EXCEPTION",
                "End Date must be on or after Start Date.",
                ["End_Date__c"],
            )


def after_save(dml: Dml, obj: SObject, rec: Record, old: Record | None) -> None:
    handler = _AFTER.get(obj.name)
    if handler:
        handler(dml, rec, old)


def after_delete(dml: Dml, obj: SObject, rec: Record) -> None:
    if obj.name == "OpportunityLineItem":
        _rollup_opportunity(dml, rec.get("OpportunityId"))
    elif obj.name in ("CampaignMember", "Opportunity"):
        _touch_campaigns(dml, rec.get("CampaignId"))


# -- people ------------------------------------------------------------------------------


def _person(dml: Dml, rec: Record, old: Record | None, provided: set[str]) -> None:
    rec["Name"] = _name(rec)


def _user(dml: Dml, rec: Record, old: Record | None, provided: set[str]) -> None:
    rec["Name"] = _name(rec)
    if old is None:
        rec["UserType"] = rec.get("UserType") or "Standard"
        rec["NumberOfFailedLogins"] = 0
        rec["IsActive"] = True if "IsActive" not in provided else rec["IsActive"]
        if not rec.get("CommunityNickname"):
            rec["CommunityNickname"] = (rec.get("Username") or "").split("@")[0]
        if not rec.get("SenderEmail"):
            rec["SenderEmail"] = rec.get("Email")
        if not rec.get("SenderName"):
            rec["SenderName"] = rec["Name"]
    username = rec.get("Username")
    if username:
        for other in dml.org.coll("User"):
            if other["Id"] != rec.get("Id") and str(other.get("Username", "")).lower() == username.lower():
                raise SfError(400, "DUPLICATE_USERNAME", "Duplicate Username.", ["Username"])


# -- opportunities -----------------------------------------------------------------------


def _fiscal(rec: Record, start_month: int) -> None:
    close = rec.get("CloseDate")
    if not close:
        rec["FiscalYear"] = rec["FiscalQuarter"] = rec["Fiscal"] = None
        return
    year, month = int(close[:4]), int(close[5:7])
    shifted = (month - start_month) % 12
    quarter = shifted // 3 + 1
    fiscal_year = year if start_month == 1 or month < start_month else year + 1
    rec["FiscalYear"], rec["FiscalQuarter"] = fiscal_year, quarter
    rec["Fiscal"] = f"{fiscal_year} {quarter}"


def _opportunity(dml: Dml, rec: Record, old: Record | None, provided: set[str]) -> None:
    stage = rec.get("StageName")
    info = stage_info(stage) if stage else stage_info("Prospecting")
    stage_changed = old is None or old.get("StageName") != stage
    if "Probability" not in provided and stage_changed:
        rec["Probability"] = float(info["probability"])
    rec["IsClosed"], rec["IsWon"] = bool(info["closed"]), bool(info["won"])
    if "ForecastCategoryName" in provided:
        rec["ForecastCategory"] = CATEGORY_BY_NAME.get(
            rec.get("ForecastCategoryName"), rec.get("ForecastCategory")
        )
    elif "ForecastCategory" in provided:
        rec["ForecastCategoryName"] = NAME_BY_CATEGORY.get(
            rec.get("ForecastCategory"), rec.get("ForecastCategoryName")
        )
    elif stage_changed:
        rec["ForecastCategory"], rec["ForecastCategoryName"] = info["category"], info["category_name"]
    amount, prob = rec.get("Amount"), rec.get("Probability")
    rec["ExpectedRevenue"] = (
        round(amount * prob / 100, 2) if amount is not None and prob is not None else None
    )
    _fiscal(rec, dml.fiscal_start)
    if stage_changed:
        rec["LastStageChangeDate"] = dml.org.now_str()
    if old is None:
        rec["HasOpportunityLineItem"] = False


def _rollup_opportunity(dml: Dml, opp_id: str | None) -> None:
    opp = dml.org.get("Opportunity", opp_id) if opp_id else None
    if opp is None:
        return
    items = [
        i
        for i in dml.org.coll("OpportunityLineItem")
        if i.get("OpportunityId") == opp["Id"] and not i.get("IsDeleted")
    ]
    dml.txn.record("Opportunity", opp)
    opp["HasOpportunityLineItem"] = bool(items)
    if items:
        opp["Amount"] = round(sum(i.get("TotalPrice") or 0 for i in items), 2)
        prob = opp.get("Probability")
        opp["ExpectedRevenue"] = round(opp["Amount"] * prob / 100, 2) if prob is not None else None
    stamp = dml.org.now_str()
    opp["LastModifiedDate"] = opp["SystemModstamp"] = stamp


def _line_item(dml: Dml, rec: Record, old: Record | None, provided: set[str]) -> None:
    entry = (
        dml.org.get("PricebookEntry", rec.get("PricebookEntryId")) if rec.get("PricebookEntryId") else None
    )
    if entry is None:
        raise SfError(
            400,
            "REQUIRED_FIELD_MISSING",
            "Required fields are missing: [PricebookEntryId]",
            ["PricebookEntryId"],
        )
    opp = dml.org.get("Opportunity", rec.get("OpportunityId"))
    if opp is not None and opp.get("Pricebook2Id") and opp["Pricebook2Id"] != entry["Pricebook2Id"]:
        raise SfError(
            400,
            "FIELD_INTEGRITY_EXCEPTION",
            "Price Book Entry ID: price book entry is not in the opportunity's price book",
            ["PricebookEntryId"],
        )
    product = dml.org.get("Product2", entry["Product2Id"]) or {}
    rec["Product2Id"] = entry["Product2Id"]
    rec["Name"] = entry.get("Name") or product.get("Name")
    rec["ProductCode"] = entry.get("ProductCode") or product.get("ProductCode")
    rec["ListPrice"] = entry.get("UnitPrice")
    quantity = rec.get("Quantity")
    unit = rec.get("UnitPrice")
    total = rec.get("TotalPrice")
    if unit is None and total is None:
        unit = entry.get("UnitPrice")
    if quantity is None:
        raise SfError(400, "REQUIRED_FIELD_MISSING", "Required fields are missing: [Quantity]", ["Quantity"])
    if quantity <= 0:
        raise SfError(
            400,
            "NUMBER_OUTSIDE_VALID_RANGE",
            f"Quantity: value outside of valid range on numeric field: {quantity:g}",
            ["Quantity"],
        )
    if "TotalPrice" in provided and "UnitPrice" not in provided and quantity:
        unit = round(total / quantity, 2)
    elif unit is not None:
        total = round(quantity * unit, 2)
    rec["UnitPrice"], rec["TotalPrice"] = unit, total
    if opp is not None and not opp.get("Pricebook2Id"):
        dml.txn.record("Opportunity", opp)
        opp["Pricebook2Id"] = entry["Pricebook2Id"]


def _line_item_after(dml: Dml, rec: Record, old: Record | None) -> None:
    _rollup_opportunity(dml, rec.get("OpportunityId"))


def _contact_role(dml: Dml, rec: Record, old: Record | None, provided: set[str]) -> None:
    if rec.get("IsPrimary"):
        for other in dml.org.coll("OpportunityContactRole"):
            if (
                other["Id"] != rec.get("Id")
                and other.get("OpportunityId") == rec.get("OpportunityId")
                and other.get("IsPrimary")
                and not other.get("IsDeleted")
            ):
                dml.txn.record("OpportunityContactRole", other)
                other["IsPrimary"] = False


# -- cases ---------------------------------------------------------------------------------


def _case(dml: Dml, rec: Record, old: Record | None, provided: set[str]) -> None:
    if old is None:
        rec["CaseNumber"] = dml.org.next_case_number()
    closed = rec.get("Status") == "Closed"
    was_closed = bool(old and old.get("IsClosed"))
    rec["IsClosed"] = closed
    if closed and not was_closed:
        rec["ClosedDate"] = dml.org.now_str()
    elif not closed:
        rec["ClosedDate"] = None
    rec["IsEscalated"] = bool(rec.get("IsEscalated")) or rec.get("Status") == "Escalated"
    contact = dml.org.get("Contact", rec.get("ContactId")) if rec.get("ContactId") else None
    if contact is not None:
        if not rec.get("AccountId"):
            rec["AccountId"] = contact.get("AccountId")
        rec["ContactPhone"], rec["ContactMobile"] = contact.get("Phone"), contact.get("MobilePhone")
        rec["ContactEmail"], rec["ContactFax"] = contact.get("Email"), contact.get("Fax")
    else:
        rec["ContactPhone"] = rec["ContactMobile"] = rec["ContactEmail"] = rec["ContactFax"] = None


# -- activities ------------------------------------------------------------------------------


def _account_for(dml: Dml, what_id: str | None) -> str | None:
    if not what_id:
        return None
    found = dml.org.find(what_id)
    if not found:
        return None
    obj, rec = found
    return rec["Id"] if obj.name == "Account" else rec.get("AccountId") or rec.get("Account__c")


def _task(dml: Dml, rec: Record, old: Record | None, provided: set[str]) -> None:
    closed = rec.get("Status") == "Completed"
    was_closed = bool(old and old.get("IsClosed"))
    rec["IsClosed"] = closed
    rec["IsHighPriority"] = rec.get("Priority") == "High"
    if closed and not was_closed and not rec.get("CompletedDateTime"):
        rec["CompletedDateTime"] = dml.org.now_str()
    elif not closed:
        rec["CompletedDateTime"] = None
    if "AccountId" not in provided:
        rec["AccountId"] = _account_for(dml, rec.get("WhatId"))
    rec["TaskSubtype"] = rec.get("TaskSubtype") or "Task"


def _event(dml: Dml, rec: Record, old: Record | None, provided: set[str]) -> None:
    start = rec.get("StartDateTime") or rec.get("ActivityDateTime")
    all_day = bool(rec.get("IsAllDayEvent"))
    if not start and all_day and rec.get("ActivityDate"):
        start = f"{rec['ActivityDate']}T00:00:00.000+0000"
    if not start:
        raise SfError(
            400, "REQUIRED_FIELD_MISSING", "Required fields are missing: [StartDateTime]", ["StartDateTime"]
        )
    start_dt = parse_dt(start)
    assert start_dt is not None
    end = rec.get("EndDateTime")
    duration = rec.get("DurationInMinutes")
    if all_day and not end and duration is None:
        duration = 1440
    if end and ("EndDateTime" in provided or old is None):
        # an explicit end always wins; the duration follows it
        end_dt = parse_dt(end)
        assert end_dt is not None
        duration = int((end_dt - start_dt).total_seconds() // 60)
    elif duration is not None:
        # a new duration, or a moved start with the old duration kept: the end follows
        end_dt = start_dt + timedelta(minutes=int(duration))
    elif end:
        end_dt = parse_dt(end)
        assert end_dt is not None
        duration = int((end_dt - start_dt).total_seconds() // 60)
    else:
        raise SfError(
            400,
            "REQUIRED_FIELD_MISSING",
            "Required fields are missing: [DurationInMinutes]",
            ["DurationInMinutes"],
        )
    if end_dt < start_dt:
        raise SfError(
            400,
            "FIELD_INTEGRITY_EXCEPTION",
            "End Date/Time: the end date and time must be later than the start date and time",
            ["EndDateTime"],
        )
    rec["StartDateTime"] = rec["ActivityDateTime"] = fmt_dt(start_dt)
    rec["EndDateTime"] = fmt_dt(end_dt)
    rec["DurationInMinutes"] = duration
    rec["ActivityDate"] = start_dt.date().isoformat()
    rec["EndDate"] = end_dt.date().isoformat()
    if "AccountId" not in provided:
        rec["AccountId"] = _account_for(dml, rec.get("WhatId"))
    rec["EventSubtype"] = rec.get("EventSubtype") or "Event"


# -- campaigns --------------------------------------------------------------------------------


def _campaign_member(dml: Dml, rec: Record, old: Record | None, provided: set[str]) -> None:
    lead_id, contact_id = rec.get("LeadId"), rec.get("ContactId")
    if bool(lead_id) == bool(contact_id):
        raise SfError(
            400,
            "REQUIRED_FIELD_MISSING",
            "Required fields are missing: [ContactId, LeadId]"
            if not lead_id
            else "Campaign members can reference a Lead or a Contact, not both",
            ["ContactId", "LeadId"],
        )
    for other in dml.org.coll("CampaignMember"):
        if (
            other["Id"] != rec.get("Id")
            and not other.get("IsDeleted")
            and other.get("CampaignId") == rec.get("CampaignId")
            and (
                (lead_id and other.get("LeadId") == lead_id)
                or (contact_id and other.get("ContactId") == contact_id)
            )
        ):
            raise SfError(400, "DUPLICATE_VALUE", "Already a campaign member.", [])
    source = dml.org.get("Lead", lead_id) if lead_id else dml.org.get("Contact", contact_id)
    if source is not None:
        account = dml.org.get("Account", source.get("AccountId")) if contact_id else None
        for key in (
            "Salutation",
            "FirstName",
            "LastName",
            "Name",
            "Title",
            "Email",
            "Phone",
            "MobilePhone",
            "Fax",
        ):
            rec[key] = source.get(key)
        rec["CompanyOrAccount"] = source.get("Company") if lead_id else (account or {}).get("Name")
        rec["LeadOrContactId"] = lead_id or contact_id
        rec["LeadOrContactOwnerId"] = source.get("OwnerId")
        rec["Type"] = "Lead" if lead_id else "Contact"
    responded = rec.get("Status") == "Responded"
    rec["HasResponded"] = responded
    if responded and not rec.get("FirstRespondedDate"):
        rec["FirstRespondedDate"] = dml.org.now().date().isoformat()


_CAMPAIGN_COUNTERS = (
    "NumberOfLeads",
    "NumberOfConvertedLeads",
    "NumberOfContacts",
    "NumberOfResponses",
    "NumberOfOpportunities",
    "NumberOfWonOpportunities",
)
_CAMPAIGN_AMOUNTS = ("AmountAllOpportunities", "AmountWonOpportunities")


def recompute_campaign(org: Any, camp: Record) -> None:
    """Refresh the roll-up summary fields of a campaign from its members and opportunities."""
    members = [
        m for m in org.coll("CampaignMember") if m["CampaignId"] == camp["Id"] and not m.get("IsDeleted")
    ]
    opps = [
        o for o in org.coll("Opportunity") if o.get("CampaignId") == camp["Id"] and not o.get("IsDeleted")
    ]
    leads = [m for m in members if m["LeadId"]]
    camp["NumberOfLeads"] = len(leads)
    camp["NumberOfConvertedLeads"] = sum(
        1 for m in leads if (org.get("Lead", m["LeadId"]) or {}).get("IsConverted")
    )
    camp["NumberOfContacts"] = sum(1 for m in members if m["ContactId"])
    camp["NumberOfResponses"] = sum(1 for m in members if m["HasResponded"])
    camp["NumberOfOpportunities"] = len(opps)
    camp["NumberOfWonOpportunities"] = sum(1 for o in opps if o["IsWon"])
    camp["AmountAllOpportunities"] = float(sum(o["Amount"] or 0 for o in opps))
    camp["AmountWonOpportunities"] = float(sum(o["Amount"] or 0 for o in opps if o["IsWon"]))


def _campaign(dml: Dml, rec: Record, old: Record | None, provided: set[str]) -> None:
    if old is None:  # roll-ups start at zero rather than null
        for key in _CAMPAIGN_COUNTERS:
            rec[key] = 0
        for key in _CAMPAIGN_AMOUNTS:
            rec[key] = 0.0


def _touch_campaigns(dml: Dml, *campaign_ids: str | None) -> None:
    for cid in dict.fromkeys(c for c in campaign_ids if c):
        camp = dml.org.get("Campaign", cid)
        if camp is not None and not camp.get("IsDeleted"):
            dml.txn.record("Campaign", camp)
            recompute_campaign(dml.org, camp)


def _member_after(dml: Dml, rec: Record, old: Record | None) -> None:
    _touch_campaigns(dml, rec.get("CampaignId"), old.get("CampaignId") if old else None)


def _opportunity_after(dml: Dml, rec: Record, old: Record | None) -> None:
    _touch_campaigns(dml, rec.get("CampaignId"), old.get("CampaignId") if old else None)


def _lead_after(dml: Dml, rec: Record, old: Record | None) -> None:
    if old is not None and old.get("IsConverted") != rec.get("IsConverted"):
        _touch_campaigns(
            dml,
            *(
                m["CampaignId"]
                for m in dml.org.coll("CampaignMember")
                if m.get("LeadId") == rec["Id"] and not m.get("IsDeleted")
            ),
        )


# -- products ---------------------------------------------------------------------------------


def _pricebook_entry(dml: Dml, rec: Record, old: Record | None, provided: set[str]) -> None:
    product = dml.org.get("Product2", rec.get("Product2Id")) or {}
    rec["Name"] = product.get("Name")
    rec["ProductCode"] = product.get("ProductCode")
    for other in dml.org.coll("PricebookEntry"):
        if (
            other["Id"] != rec.get("Id")
            and not other.get("IsDeleted")
            and other.get("Pricebook2Id") == rec.get("Pricebook2Id")
            and other.get("Product2Id") == rec.get("Product2Id")
        ):
            raise SfError(
                400,
                "DUPLICATE_VALUE",
                f"duplicate value found: <unknown> duplicates value on record with id: {other['Id']}",
                [],
            )


# -- files ---------------------------------------------------------------------------------------

FILE_TYPES = {
    "docx": "WORD_X", "doc": "WORD", "xlsx": "EXCEL_X", "xls": "EXCEL", "pptx": "POWER_POINT_X",
    "ppt": "POWER_POINT", "pdf": "PDF", "csv": "CSV", "txt": "TEXT", "md": "TEXT", "png": "PNG",
    "jpg": "JPG", "jpeg": "JPG", "gif": "GIF", "zip": "ZIP", "json": "JSON", "xml": "XML", "html": "HTML",
}  # fmt: skip


def file_extension(name: str | None) -> str | None:
    leaf = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return leaf.rsplit(".", 1)[-1].lower() if "." in leaf else None


def file_title(path: str | None) -> str:
    leaf = (path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return leaf.rsplit(".", 1)[0] if "." in leaf else leaf


def _content_version(dml: Dml, rec: Record, old: Record | None, provided: set[str]) -> None:
    if old is not None:
        return
    key = str(rec.get("VersionData") or "").removeprefix("blob:")
    data = dml.org.store.blobs.get(key) if key else b""
    ext = file_extension(rec.get("PathOnClient"))
    rec["ContentSize"] = len(data)
    rec["Checksum"] = hashlib.md5(data, usedforsecurity=False).hexdigest()
    rec["FileExtension"] = ext
    rec["FileType"] = FILE_TYPES.get(ext or "", "UNKNOWN")
    rec["IsLatest"] = True
    rec["VersionNumber"] = "1"
    rec["RatingCount"] = rec["PositiveRatingCount"] = rec["NegativeRatingCount"] = 0
    rec["ContentModifiedDate"] = rec.get("ContentModifiedDate") or dml.org.now_str()
    rec["ContentModifiedById"] = dml.access.user["Id"]
    rec["Title"] = rec.get("Title") or file_title(rec.get("PathOnClient"))
    if rec.get("ContentDocumentId"):
        doc = dml.org.get("ContentDocument", rec["ContentDocumentId"])
        if doc is None or doc.get("IsDeleted"):
            raise SfError(
                400, "INVALID_CROSS_REFERENCE_KEY", "invalid cross reference id", ["ContentDocumentId"]
            )
        if not dml.system and not dml.access.can_edit(SCHEMA.objects["ContentDocument"], doc):
            raise SfError(
                400,
                "INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY",
                f"insufficient access rights on cross-reference id: {doc['Id']}",
                ["ContentDocumentId"],
            )


def _link(dml: Dml, doc_id: str, entity_id: str, share_type: str, visibility: str = "AllUsers") -> Record:
    org = dml.org
    link = blank(SCHEMA.objects["ContentDocumentLink"])
    link.update(
        Id=org.new_id("ContentDocumentLink"),
        LinkedEntityId=entity_id,
        ContentDocumentId=doc_id,
        ShareType=share_type,
        Visibility=visibility,
        IsDeleted=False,
        SystemModstamp=org.now_str(),
    )
    dml.txn.record("ContentDocumentLink", None, created_id=link["Id"])
    org.put("ContentDocumentLink", link)
    return link


def _content_version_after(dml: Dml, rec: Record, old: Record | None) -> None:
    org = dml.org
    now = org.now_str()
    if old is not None:
        doc = org.get("ContentDocument", rec.get("ContentDocumentId"))
        changed = old.get("Title") != rec.get("Title") or old.get("Description") != rec.get("Description")
        if doc is not None and changed:
            dml.txn.record("ContentDocument", doc)
            doc.update(
                Title=rec["Title"],
                Description=rec.get("Description"),
                LastModifiedDate=now,
                SystemModstamp=now,
            )
        return
    user_id = dml.access.user["Id"]
    if rec.get("ContentDocumentId"):
        doc = org.get("ContentDocument", rec["ContentDocumentId"])
        assert doc is not None
        dml.txn.record("ContentDocument", doc)
        versions = [
            v
            for v in org.coll("ContentVersion")
            if v.get("ContentDocumentId") == doc["Id"] and v["Id"] != rec["Id"] and not v.get("IsDeleted")
        ]
        for v in versions:
            dml.txn.record("ContentVersion", v)
            v["IsLatest"] = False
        rec["VersionNumber"] = str(len(versions) + 1)
        doc.update(
            Title=rec["Title"],
            LatestPublishedVersionId=rec["Id"],
            ContentSize=rec["ContentSize"],
            FileType=rec["FileType"],
            FileExtension=rec["FileExtension"],
            ContentModifiedDate=rec["ContentModifiedDate"],
            LastModifiedDate=now,
            LastModifiedById=user_id,
            SystemModstamp=now,
        )
        return
    doc = blank(SCHEMA.objects["ContentDocument"])
    doc.update(
        Id=org.new_id("ContentDocument"),
        IsDeleted=False,
        OwnerId=rec["OwnerId"],
        CreatedDate=now,
        CreatedById=user_id,
        LastModifiedDate=now,
        LastModifiedById=user_id,
        SystemModstamp=now,
        Title=rec["Title"],
        PublishStatus="R",
        LatestPublishedVersionId=rec["Id"],
        Description=rec.get("Description"),
        ContentSize=rec["ContentSize"],
        FileType=rec["FileType"],
        FileExtension=rec["FileExtension"],
        ContentModifiedDate=rec["ContentModifiedDate"],
        IsArchived=False,
        SharingOption=rec.get("SharingOption") or "A",
        SharingPrivacy=rec.get("SharingPrivacy") or "N",
    )
    dml.txn.record("ContentDocument", None, created_id=doc["Id"])
    org.put("ContentDocument", doc)
    rec["ContentDocumentId"] = doc["Id"]
    _link(dml, doc["Id"], rec["OwnerId"], "I")  # the owner's personal library
    if rec.get("FirstPublishLocationId") and rec["FirstPublishLocationId"] != rec["OwnerId"]:
        _link(dml, doc["Id"], rec["FirstPublishLocationId"], "V")


def _content_link(dml: Dml, rec: Record, old: Record | None, provided: set[str]) -> None:
    if old is not None:
        return
    doc = dml.org.get("ContentDocument", rec.get("ContentDocumentId"))
    if doc is None or doc.get("IsDeleted"):
        raise SfError(400, "INVALID_CROSS_REFERENCE_KEY", "invalid cross reference id", ["ContentDocumentId"])
    for other in dml.org.coll("ContentDocumentLink"):
        if (
            not other.get("IsDeleted")
            and other.get("ContentDocumentId") == rec.get("ContentDocumentId")
            and other.get("LinkedEntityId") == rec.get("LinkedEntityId")
        ):
            raise SfError(
                400,
                "DUPLICATE_VALUE",
                f"duplicate value found: <unknown> duplicates value on record with id: {other['Id']}",
                [],
            )
    rec["Visibility"] = rec.get("Visibility") or "AllUsers"


def _attachment(dml: Dml, rec: Record, old: Record | None, provided: set[str]) -> None:
    if "Body" in provided or old is None:
        rec["BodyLength"] = int(rec.get("_size_Body") or 0)
    if not rec.get("ContentType"):
        rec["ContentType"] = mimetypes.guess_type(rec.get("Name") or "")[0] or "application/octet-stream"


_BEFORE: dict[str, Any] = {
    "ContentVersion": _content_version,
    "ContentDocumentLink": _content_link,
    "Attachment": _attachment,
    "Contact": _person,
    "Lead": _person,
    "User": _user,
    "Opportunity": _opportunity,
    "OpportunityLineItem": _line_item,
    "OpportunityContactRole": _contact_role,
    "Case": _case,
    "Task": _task,
    "Event": _event,
    "Campaign": _campaign,
    "CampaignMember": _campaign_member,
    "PricebookEntry": _pricebook_entry,
}
_AFTER: dict[str, Any] = {
    "OpportunityLineItem": _line_item_after,
    "ContentVersion": _content_version_after,
    "CampaignMember": _member_after,
    "Opportunity": _opportunity_after,
    "Lead": _lead_after,
}
