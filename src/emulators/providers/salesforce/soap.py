"""SOAP login (partner and enterprise WSDLs): ``login``, ``logout`` and ``getUserInfo``.

``simple-salesforce`` (and most JVM/.NET tooling) authenticates with username + password +
security token through ``POST /services/Soap/u/{version}``. Other SOAP API operations are not
implemented; they return a SOAP fault.
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request, Response

from emulators.core.auth import bearer_token
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.salesforce import auth as sfauth
from emulators.providers.salesforce.common import INSTANCE_HOST, parse_version
from emulators.providers.salesforce.org import Org, Record

MAX_BODY = 256 * 1024
ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns="{ns}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:sf="urn:fault.{kind}.soap.sforce.com">{body}</soapenv:Envelope>'
)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(root: ET.Element, name: str) -> ET.Element | None:
    for el in root.iter():
        if _local(el.tag) == name:
            return el
    return None


def _text(root: ET.Element | None, name: str) -> str | None:
    if root is None:
        return None
    el = _find(root, name)
    return el.text if el is not None else None


def soap_fault(code: str, message: str, kind: str = "partner") -> Response:
    body = (
        "<soapenv:Body><soapenv:Fault>"
        f"<faultcode>sf:{code}</faultcode><faultstring>{code}: {escape(message)}</faultstring>"
        '<detail><sf:LoginFault xsi:type="sf:LoginFault">'
        f"<sf:exceptionCode>{code}</sf:exceptionCode><sf:exceptionMessage>{escape(message)}</sf:exceptionMessage>"
        "</sf:LoginFault></detail></soapenv:Fault></soapenv:Body>"
    )
    xml = ENVELOPE.format(ns=f"urn:{kind}.soap.sforce.com", kind=kind, body=body)
    return Response(xml, status_code=500, media_type="text/xml;charset=UTF-8")


def user_info_xml(org: Org, user: Record) -> str:
    def tag(name: str, value: Any) -> str:
        if value is None:
            return f'<{name} xsi:nil="true"/>'
        text = str(value).lower() if isinstance(value, bool) else str(value)
        return f"<{name}>{escape(text)}</{name}>"

    return (
        "<userInfo>"
        + tag("accessibilityMode", False)
        + tag("chatterExternal", False)
        + tag("currencySymbol", "$")
        + tag("orgAttachmentFileSizeLimit", 5242880)
        + tag("orgDefaultCurrencyIsoCode", "USD")
        + tag("orgDefaultCurrencyLocale", "en_US")
        + tag("orgDisallowHtmlAttachments", False)
        + tag("orgHasPersonAccounts", False)
        + tag("organizationId", org.org_id18)
        + tag("organizationMultiCurrency", False)
        + tag("organizationName", org.world.org.name)
        + tag("profileId", user.get("ProfileId"))
        + tag("roleId", user.get("UserRoleId"))
        + tag("sessionSecondsValid", 7200)
        + tag("userDefaultCurrencyIsoCode", None)
        + tag("userEmail", user.get("Email"))
        + tag("userFullName", user.get("Name"))
        + tag("userId", user["Id"])
        + tag("userLanguage", user.get("LanguageLocaleKey"))
        + tag("userLocale", user.get("LocaleSidKey"))
        + tag("userName", user.get("Username"))
        + tag("userTimeZone", user.get("TimeZoneSidKey"))
        + tag("userType", user.get("UserType"))
        + tag("userUiSkin", "Theme3")
        + "</userInfo>"
    )


def register(app: FastAPI, ctx: ProviderContext, org: Org) -> None:
    @app.post("/services/Soap/{kind}/{version}", include_in_schema=False)
    @app.post("/services/Soap/{kind}/{version}/{org_id}", include_in_schema=False)
    async def soap(kind: str, version: str, request: Request, org_id: str | None = None) -> Response:
        flavour = {"u": "partner", "c": "enterprise"}.get(kind)
        if flavour is None:
            return soap_fault(
                "UNSUPPORTED_API_VERSION", f"The {kind} SOAP API is not supported by this emulator."
            )
        ns = f"urn:{flavour}.soap.sforce.com"
        raw = await request.body()
        if len(raw) > MAX_BODY or b"<!DOCTYPE" in raw[:2048].upper() or b"<!ENTITY" in raw.upper():
            return soap_fault("INVALID_REQUEST", "Malformed SOAP request", flavour)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return soap_fault("INVALID_REQUEST", "Malformed SOAP request", flavour)
        if parse_version(f"v{version}") is None:
            return soap_fault("UNSUPPORTED_API_VERSION", f"Invalid API version {version}", flavour)
        body = _find(root, "Body")
        operation = next((_local(c.tag) for c in (list(body) if body is not None else [])), "")
        base = url_for_host(request, ctx, INSTANCE_HOST, "").rstrip("/")
        if operation == "login":
            return _login(request, root, flavour, ns, version, base)
        if operation in ("getUserInfo", "logout"):
            return _session_call(request, root, operation, flavour, ns)
        return soap_fault(
            "UNSUPPORTED_API_VERSION",
            f"Operation '{operation}' is not implemented by this emulator.",
            flavour,
        )

    def _login(
        request: Request, root: ET.Element, flavour: str, ns: str, version: str, base: str
    ) -> Response:
        login = _find(root, "login")
        username = _text(login, "username")
        password = _text(login, "password") or ""
        user = sfauth.user_by_login(org, username)
        invalid = soap_fault(
            "INVALID_LOGIN", "Invalid username, password, security token; or user locked out.", flavour
        )
        if user is None or not user.get("IsActive") or not sfauth.check_password(org, ctx, user, password):
            sfauth.record_failed_login(user)
            return invalid
        client = _text(root, "client") or "soap"
        grant = sfauth.issue_session(ctx, org, user, client_id=client, scopes=["api", "full"])
        oid = org.org_id18
        result = (
            "<soapenv:Body><loginResponse><result>"
            f"<metadataServerUrl>{base}/services/Soap/m/{version}/{oid}</metadataServerUrl>"
            "<passwordExpired>false</passwordExpired><sandbox>false</sandbox>"
            f"<serverUrl>{base}/services/Soap/{'u' if flavour == 'partner' else 'c'}/{version}/{oid}</serverUrl>"
            f"<sessionId>{grant.token}</sessionId><userId>{user['Id']}</userId>"
            f"{user_info_xml(org, user)}</result></loginResponse></soapenv:Body>"
        )
        return Response(
            ENVELOPE.format(ns=ns, kind=flavour, body=result), media_type="text/xml;charset=UTF-8"
        )

    def _session_call(request: Request, root: ET.Element, op: str, flavour: str, ns: str) -> Response:
        session_id = _text(root, "sessionId") or bearer_token(request)
        grant = ctx.tokens.resolve(session_id) if session_id else None
        user = org.find_user(grant.user_id) if grant and grant.user_id else None
        if grant is None or user is None:
            return soap_fault(
                "INVALID_SESSION_ID", "Invalid Session ID found in SessionHeader: Illegal Session", flavour
            )
        if op == "logout":
            ctx.tokens.revoke(grant.token)
            body = "<soapenv:Body><logoutResponse/></soapenv:Body>"
        else:
            body = f"<soapenv:Body><getUserInfoResponse><result>{user_info_xml(org, user)[len('<userInfo>') : -len('</userInfo>')]}</result></getUserInfoResponse></soapenv:Body>"
        return Response(ENVELOPE.format(ns=ns, kind=flavour, body=body), media_type="text/xml;charset=UTF-8")
