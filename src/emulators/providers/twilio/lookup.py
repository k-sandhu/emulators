"""Lookup v2 (``lookups.twilio.com/v2/PhoneNumbers``): validation, caller name, line type."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request

from emulators.providers.twilio.common import (
    LOOKUPS_HOST,
    absolute,
    geo_for,
    national_format,
    not_found,
    require_auth,
)

if TYPE_CHECKING:
    from emulators.core.provider import ProviderContext

Record = dict[str, Any]

# Country calling code -> (default ISO country, minimum / maximum national-number digits).
CALLING_CODES: dict[str, tuple[str, int, int]] = {
    "1": ("US", 10, 10),
    "7": ("RU", 10, 10),
    "20": ("EG", 8, 10),
    "27": ("ZA", 9, 9),
    "30": ("GR", 10, 10),
    "31": ("NL", 9, 9),
    "32": ("BE", 8, 9),
    "33": ("FR", 9, 9),
    "34": ("ES", 9, 9),
    "36": ("HU", 8, 9),
    "39": ("IT", 6, 11),
    "41": ("CH", 9, 9),
    "43": ("AT", 4, 13),
    "44": ("GB", 9, 10),
    "45": ("DK", 8, 8),
    "46": ("SE", 7, 13),
    "47": ("NO", 5, 8),
    "48": ("PL", 9, 9),
    "49": ("DE", 5, 13),
    "52": ("MX", 10, 10),
    "54": ("AR", 10, 11),
    "55": ("BR", 10, 11),
    "56": ("CL", 8, 9),
    "57": ("CO", 8, 10),
    "60": ("MY", 7, 10),
    "61": ("AU", 9, 9),
    "62": ("ID", 8, 12),
    "63": ("PH", 8, 10),
    "64": ("NZ", 8, 10),
    "65": ("SG", 8, 8),
    "66": ("TH", 8, 9),
    "81": ("JP", 9, 10),
    "82": ("KR", 8, 10),
    "84": ("VN", 8, 10),
    "86": ("CN", 8, 12),
    "90": ("TR", 10, 10),
    "91": ("IN", 10, 10),
    "92": ("PK", 9, 10),
    "234": ("NG", 7, 10),
    "254": ("KE", 9, 9),
    "351": ("PT", 9, 9),
    "353": ("IE", 7, 9),
    "358": ("FI", 5, 12),
    "852": ("HK", 8, 8),
    "966": ("SA", 8, 9),
    "971": ("AE", 8, 9),
    "972": ("IL", 8, 9),
}
_CODE_FOR_COUNTRY = {"CA": "1", **{v[0]: k for k, v in CALLING_CODES.items()}}
MAGIC_INVALID = "+15005550001"


@dataclass
class ParsedNumber:
    e164: str
    calling_code: str = ""
    country: str = ""
    valid: bool = False
    errors: list[str] = field(default_factory=list)


def parse_phone(raw: str, default_country: str = "US") -> ParsedNumber:
    """Parse E.164 or national input the way Lookup does, reporting ``validation_errors``.

    The error vocabulary is Lookup v2's: ``NOT_A_NUMBER``, ``INVALID_COUNTRY_CODE``,
    ``TOO_SHORT``, ``TOO_LONG`` and ``INVALID_BUT_POSSIBLE``.
    """
    text = raw.strip()
    cleaned = re.sub(r"[\s().\-]", "", text)
    if not re.fullmatch(r"\+?\d+", cleaned):
        return ParsedNumber(text, errors=["NOT_A_NUMBER"])
    if cleaned.startswith("+"):
        digits = cleaned[1:]
        code = next((digits[:n] for n in (3, 2, 1) if digits[:n] in CALLING_CODES), "")
        if not code:
            return ParsedNumber("+" + digits, errors=["INVALID_COUNTRY_CODE"])
        national = digits[len(code) :]
    else:
        country = default_country.upper()
        code = _CODE_FOR_COUNTRY.get(country, "")
        if not code:
            return ParsedNumber(cleaned, errors=["INVALID_COUNTRY_CODE"])
        national = cleaned
        if code == "1" and len(national) == 11 and national.startswith("1"):
            national = national[1:]
        elif code != "1":
            national = national.lstrip("0")  # national trunk prefix
    default_iso, lo, hi = CALLING_CODES[code]
    e164 = f"+{code}{national}"
    errors: list[str] = []
    if len(national) < lo:
        errors.append("TOO_SHORT")
    elif len(national) > hi:
        errors.append("TOO_LONG")
    elif code == "1" and not re.fullmatch(r"[2-9]\d{2}[2-9]\d{6}", national):
        errors.append("INVALID_BUT_POSSIBLE")
    if e164 == MAGIC_INVALID:
        errors = ["INVALID_BUT_POSSIBLE"]
    iso = geo_for(e164)[2] or default_iso if code == "1" else default_iso
    return ParsedNumber(e164, code, iso, not errors, errors)


def _people_by_phone(ctx: ProviderContext) -> dict[str, tuple[str, str]]:
    book: dict[str, tuple[str, str]] = {}
    for person in ctx.world.people:
        if person.phone_mobile:
            book[person.phone_mobile] = (person.display_name, "mobile")
        if person.phone_business:
            book.setdefault(person.phone_business, (person.display_name, "landline"))
    for lead in ctx.world.leads:
        book.setdefault(lead.phone, (f"{lead.first_name} {lead.last_name}", "mobile"))
    for office in ctx.world.offices:
        book.setdefault(office.phone, (ctx.world.org.name, "landline"))
    book.setdefault(ctx.world.org.phone, (ctx.world.org.name, "landline"))
    return book


def _identity(ctx: ProviderContext, number: str) -> tuple[str | None, str | None]:
    """(caller name, line type) known to the synthetic world for ``number``."""
    name, line_type = _people_by_phone(ctx).get(number, (None, None))
    if number in {n["phone_number"] for n in ctx.store.collection("numbers").values()}:
        return name or ctx.world.org.name, "nonFixedVoip"
    return name, line_type


def _carrier(line_type: str | None, country: str) -> tuple[str | None, str | None, str | None]:
    canada = country == "CA"
    if line_type == "mobile":
        return (
            "Rogers Communications Canada Inc." if canada else "T-Mobile USA, Inc.",
            "302" if canada else "310",
            "720" if canada else "260",
        )
    if line_type == "landline":
        return ("Bell Canada" if canada else "Verizon", None, None)
    if line_type == "nonFixedVoip":
        return ("Twilio Inc.", None, None)
    return (None, None, None)


def carrier_info(ctx: ProviderContext, number: str) -> Record:
    """The ``carrier`` object nested in Verify's ``lookup`` field."""
    parsed = parse_phone(number)
    _, line_type = _identity(ctx, parsed.e164)
    if parsed.valid and line_type is None:
        line_type = "mobile"
    name, mcc, mnc = _carrier(line_type if parsed.valid else None, parsed.country)
    return {
        "carrier": {
            "error_code": None,
            "name": name,
            "mobile_country_code": mcc,
            "mobile_network_code": mnc,
            "type": line_type if parsed.valid and line_type != "nonFixedVoip" else None,
        }
    }


def lookup_phone(ctx: ProviderContext, request: Request, raw_number: str) -> Record:
    fields = {f.strip().lower() for f in (request.query_params.get("Fields") or "").split(",") if f.strip()}
    parsed = parse_phone(raw_number, request.query_params.get("CountryCode") or "US")
    valid = parsed.valid
    known_name, known_type = _identity(ctx, parsed.e164) if valid else (None, None)
    line_type = known_type or ("mobile" if valid else None)
    result: Record = {
        "calling_country_code": parsed.calling_code or None,
        "country_code": parsed.country or None,
        "phone_number": parsed.e164,
        "national_format": national_format(parsed.e164) if valid else None,
        "valid": valid,
        "validation_errors": None if valid else parsed.errors,
        "caller_name": None,
        "sim_swap": None,
        "call_forwarding": None,
        "line_type_intelligence": None,
        "line_status": None,
        "identity_match": None,
        "reassigned_number": None,
        "sms_pumping_risk": None,
        "phone_number_quality_score": None,
        "pre_fill": None,
        "url": absolute(request, ctx, LOOKUPS_HOST, f"/v2/PhoneNumbers/{parsed.e164}"),
    }
    if "caller_name" in fields:
        business = known_name == ctx.world.org.name
        result["caller_name"] = {
            "caller_name": known_name.upper() if known_name else None,
            "caller_type": ("BUSINESS" if business else "CONSUMER") if known_name else None,
            "error_code": None,
        }
    if "line_type_intelligence" in fields:
        carrier, mcc, mnc = _carrier(line_type, parsed.country)
        result["line_type_intelligence"] = {
            "carrier_name": carrier if valid else None,
            "type": line_type if valid else None,
            "mobile_country_code": mcc if valid else None,
            "mobile_network_code": mnc if valid else None,
            "error_code": None,
        }
    if "line_status" in fields:
        result["line_status"] = {"status": "active" if valid else "unknown", "error_code": None}
    if "sms_pumping_risk" in fields:
        result["sms_pumping_risk"] = {
            "sms_pumping_risk_score": 5 if valid else 100,
            "carrier_risk_category": "low" if valid else "high",
            "blocked": False,
            "error_code": None,
        }
    if "sim_swap" in fields:
        result["sim_swap"] = {"last_sim_swap": {"last_swap_date": None}, "error_code": None}
    return result


def lookup_phone_v1(ctx: ProviderContext, request: Request, raw_number: str) -> Record:
    """Lookup v1 (``client.lookups.phone_numbers(...)`` in the SDKs): 404 for invalid numbers."""
    parsed = parse_phone(raw_number, request.query_params.get("CountryCode") or "US")
    if not parsed.valid:
        raise not_found(f"/PhoneNumbers/{raw_number}")
    types = {t.lower() for t in request.query_params.getlist("Type")}
    name, known_type = _identity(ctx, parsed.e164)
    line_type = known_type or "mobile"
    result: Record = {
        "caller_name": None,
        "carrier": None,
        "country_code": parsed.country,
        "national_format": national_format(parsed.e164),
        "phone_number": parsed.e164,
        "add_ons": None,
        "url": absolute(request, ctx, LOOKUPS_HOST, f"/v1/PhoneNumbers/{parsed.e164}"),
    }
    if "caller-name" in types:
        result["caller_name"] = {
            "caller_name": name.upper() if name else None,
            "caller_type": ("BUSINESS" if name == ctx.world.org.name else "CONSUMER") if name else None,
            "error_code": None,
        }
    if "carrier" in types:
        carrier, mcc, mnc = _carrier(line_type, parsed.country)
        result["carrier"] = {
            "mobile_country_code": mcc,
            "mobile_network_code": mnc,
            "name": carrier,
            "type": "voip" if line_type == "nonFixedVoip" else line_type,
            "error_code": None,
        }
    return result


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/v2/PhoneNumbers/{phone_number:path}")
    async def lookup(request: Request, phone_number: str) -> Any:
        require_auth(request, ctx)
        return lookup_phone(ctx, request, phone_number)

    @app.get("/v1/PhoneNumbers/{phone_number:path}")
    async def lookup_v1(request: Request, phone_number: str) -> Any:
        require_auth(request, ctx)
        return lookup_phone_v1(ctx, request, phone_number)


__all__ = ["carrier_info", "lookup_phone", "lookup_phone_v1", "parse_phone", "register"]
