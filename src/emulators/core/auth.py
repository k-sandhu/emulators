"""Shared OAuth 2.0 / OIDC machinery.

Every provider speaks a slightly different dialect of OAuth (Microsoft v2.0 endpoints,
Google's ``oauth2.googleapis.com``, Dropbox's ``/oauth2/token``, Salesforce's
``instance_url``, Zoom's ``account_credentials`` grant...). The *mechanics* are shared:

* a client registry (lenient by default: unknown clients are auto-registered, so any
  ``client_id``/``client_secret`` pair works unless a test opts into strict mode);
* authorization codes with PKCE (S256 and plain) and ``redirect_uri`` binding;
* access/refresh tokens bound to a user (delegated) or to the app (client credentials);
* RS256 JWT signing with a JWKS document for providers that issue JWTs.

The authorize endpoint is headless-friendly: pass ``login_hint=<email>`` and it redirects
straight back with a code. Without a hint it renders a small "choose an account" page.

Grants live in the provider :class:`~emulators.core.store.Store` (collection
``_grants``), so ``reset`` and snapshots cover them.
"""

from __future__ import annotations

import base64
import hashlib
import html
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request

from emulators.core.clock import Clock, utc
from emulators.core.store import Store

GRANTS = "_grants"
CLIENTS = "_oauth_clients"


class OAuthError(Exception):
    """RFC 6749 error; providers render it in their own token-endpoint format."""

    def __init__(self, error: str, description: str = "", status: int = 400) -> None:
        super().__init__(f"{error}: {description}")
        self.error = error
        self.description = description
        self.status = status


@dataclass
class Grant:
    token: str
    kind: str  # "access" | "refresh" | "code"
    client_id: str
    user_id: str | None
    scopes: list[str]
    issued_at: datetime
    expires_at: datetime | None
    app_only: bool = False
    redirect_uri: str | None = None
    code_challenge: str | None = None
    code_challenge_method: str | None = None
    nonce: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    revoked: bool = False

    def to_record(self) -> dict[str, Any]:
        record = dict(self.__dict__)
        record["id"] = self.token
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Grant:
        data = {k: v for k, v in record.items() if k != "id"}
        return cls(**data)

    def has_scope(self, *wanted: str) -> bool:
        return any(w in self.scopes for w in wanted)


def verify_pkce(verifier: str | None, challenge: str | None, method: str | None) -> bool:
    if challenge is None:
        return True
    if not verifier:
        return False
    if (method or "plain").upper() == "S256":
        digest = hashlib.sha256(verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge
    return verifier == challenge


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() in ("bearer", "oauth") and value.strip():
        return value.strip()
    return None


def basic_credentials(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(value.strip()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    user, sep, password = decoded.partition(":")
    return (user, password) if sep else None


class TokenService:
    """Issues and resolves grants for one provider."""

    def __init__(self, store: Store, clock: Clock, *, lenient_clients: bool = True) -> None:
        self.store = store
        self.clock = clock
        self.lenient_clients = lenient_clients
        self._lock = threading.Lock()

    # -- clients -------------------------------------------------------------------
    def register_client(
        self,
        client_id: str,
        client_secret: str | None = None,
        *,
        redirect_uris: list[str] | None = None,
        name: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        record = {
            "id": client_id,
            "client_secret": client_secret,
            "redirect_uris": redirect_uris or [],
            "name": name or client_id,
            **extra,
        }
        return self.store.collection(CLIENTS).put(record)

    def client(self, client_id: str) -> dict[str, Any] | None:
        return self.store.collection(CLIENTS).get(client_id)

    def authenticate_client(
        self, client_id: str | None, client_secret: str | None, *, require_secret: bool = True
    ) -> dict[str, Any]:
        if not client_id:
            raise OAuthError("invalid_client", "Client authentication failed: missing client_id.", 401)
        record = self.client(client_id)
        if record is None:
            if not self.lenient_clients:
                raise OAuthError("invalid_client", f"Unknown client '{client_id}'.", 401)
            record = self.register_client(client_id, client_secret)
        expected = record.get("client_secret")
        if require_secret and expected and client_secret != expected and not self.lenient_clients:
            raise OAuthError("invalid_client", "Invalid client secret provided.", 401)
        return record

    # -- grants --------------------------------------------------------------------
    def _save(self, grant: Grant) -> Grant:
        self.store.collection(GRANTS).put(grant.to_record())
        return grant

    def issue(
        self,
        kind: str,
        *,
        client_id: str,
        user_id: str | None,
        scopes: list[str] | None = None,
        ttl: timedelta | None = timedelta(hours=1),
        token: str | None = None,
        app_only: bool = False,
        prefix: str = "",
        length: int = 48,
        **extra_fields: Any,
    ) -> Grant:
        now = self.clock.now()
        grant = Grant(
            token=token or prefix + secrets.token_urlsafe(length)[:length],
            kind=kind,
            client_id=client_id,
            user_id=user_id,
            scopes=list(scopes or []),
            issued_at=now,
            expires_at=(now + ttl) if ttl is not None else None,
            app_only=app_only,
            **extra_fields,
        )
        return self._save(grant)

    def issue_access(self, **kwargs: Any) -> Grant:
        return self.issue("access", **kwargs)

    def issue_refresh(self, **kwargs: Any) -> Grant:
        kwargs.setdefault("ttl", timedelta(days=90))
        return self.issue("refresh", **kwargs)

    def issue_code(
        self,
        *,
        client_id: str,
        user_id: str,
        scopes: list[str],
        redirect_uri: str | None,
        code_challenge: str | None = None,
        code_challenge_method: str | None = None,
        nonce: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Grant:
        return self.issue(
            "code",
            client_id=client_id,
            user_id=user_id,
            scopes=scopes,
            ttl=timedelta(minutes=10),
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            nonce=nonce,
            extra=extra or {},
            length=64,
        )

    def get(self, token: str | None, kind: str | None = None) -> Grant | None:
        if not token:
            return None
        record = self.store.collection(GRANTS).get(token)
        if record is None:
            return None
        grant = Grant.from_record(record)
        if kind and grant.kind != kind:
            return None
        return grant

    def resolve(self, token: str | None, kind: str = "access") -> Grant | None:
        """Return a live grant for ``token`` or ``None`` (missing, revoked or expired)."""
        grant = self.get(token, kind)
        if grant is None or grant.revoked:
            return None
        if grant.expires_at is not None and utc(grant.expires_at) <= self.clock.now():
            return None
        return grant

    def is_expired(self, token: str | None) -> bool:
        grant = self.get(token)
        return bool(grant and grant.expires_at and utc(grant.expires_at) <= self.clock.now())

    def redeem_code(
        self, code: str | None, *, client_id: str | None, redirect_uri: str | None, code_verifier: str | None
    ) -> Grant:
        grant = self.resolve(code, "code")
        if grant is None:
            raise OAuthError("invalid_grant", "The authorization code is invalid or has expired.")
        if client_id and grant.client_id != client_id:
            raise OAuthError("invalid_grant", "The authorization code was issued to a different client.")
        if grant.redirect_uri and redirect_uri and grant.redirect_uri != redirect_uri:
            raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request.")
        if not verify_pkce(code_verifier, grant.code_challenge, grant.code_challenge_method):
            raise OAuthError("invalid_grant", "PKCE verification failed: code_verifier does not match.")
        self.revoke(grant.token)  # codes are single use
        return grant

    def revoke(self, token: str) -> bool:
        record = self.store.collection(GRANTS).get(token)
        if record is None:
            return False
        record["revoked"] = True
        return True

    def revoke_user(self, user_id: str) -> int:
        count = 0
        for record in self.store.collection(GRANTS):
            if record.get("user_id") == user_id and not record.get("revoked"):
                record["revoked"] = True
                count += 1
        return count


class SigningKey:
    """An RSA key that signs JWTs and publishes itself as a JWKS entry.

    One key per process is plenty; it is generated lazily because RSA keygen costs tens
    of milliseconds and many test runs never need JWTs.
    """

    _shared: SigningKey | None = None
    _lock = threading.Lock()

    def __init__(self, kid: str = "emulators-rs256-1") -> None:
        self.kid = kid
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_pem = (
            self._private.public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode()
        )

    @classmethod
    def shared(cls) -> SigningKey:
        with cls._lock:
            if cls._shared is None:
                cls._shared = cls()
            return cls._shared

    def sign(self, claims: dict[str, Any], *, headers: dict[str, Any] | None = None) -> str:
        return jwt.encode(
            claims, self._private, algorithm="RS256", headers={"kid": self.kid, **(headers or {})}
        )

    def verify(self, token: str, *, audience: str | None = None) -> dict[str, Any]:
        return jwt.decode(
            token,
            self.public_pem,
            algorithms=["RS256"],
            audience=audience,
            options={"verify_aud": audience is not None},
        )

    def jwk(self) -> dict[str, Any]:
        numbers = self._private.public_key().public_numbers()

        def b64(n: int) -> str:
            raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        return {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": self.kid,
            "n": b64(numbers.n),
            "e": b64(numbers.e),
        }

    def jwks(self) -> dict[str, Any]:
        return {"keys": [self.jwk()]}


def redirect_with_params(redirect_uri: str, params: dict[str, Any], *, fragment: bool = False) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    sep = "#" if fragment else ("&" if "?" in redirect_uri else "?")
    return f"{redirect_uri}{sep}{urlencode(clean)}"


def render_account_picker(
    *,
    provider_title: str,
    app_name: str,
    scopes: list[str],
    accounts: list[dict[str, str]],
    action_base: str,
) -> str:
    """A minimal 'choose an account' consent page.

    ``accounts`` items need ``display_name``, ``email`` and ``login_hint``. Each choice is
    a link back to the authorize URL with ``login_hint`` set, so no form handling or
    cookies are needed and a browser-driven OAuth test can simply click a name.
    """
    sep = "&" if "?" in action_base else "?"
    items = "\n".join(
        f'<li><a href="{html.escape(action_base + sep + urlencode({"login_hint": a["login_hint"]}))}">'
        f"<strong>{html.escape(a['display_name'])}</strong><span>{html.escape(a['email'])}</span></a></li>"
        for a in accounts
    )
    scope_items = "".join(f"<li>{html.escape(s)}</li>" for s in scopes) or "<li>(default scopes)</li>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in - {html.escape(provider_title)} (emulator)</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f5f6f8;margin:0;padding:24px;color:#1c1e21}}
main{{max-width:560px;margin:0 auto;background:#fff;border-radius:10px;padding:24px;box-shadow:0 1px 3px #0002}}
h1{{font-size:20px;margin:0 0 4px}} p{{color:#555;margin:0 0 16px}}
ul.accounts{{list-style:none;padding:0;margin:0;max-height:420px;overflow:auto}}
ul.accounts a{{display:flex;flex-direction:column;padding:10px 12px;border:1px solid #e3e5e8;border-radius:8px;
margin-bottom:8px;text-decoration:none;color:inherit}} ul.accounts a:hover{{background:#eef4ff}}
ul.accounts span{{color:#666;font-size:13px}} .badge{{font-size:12px;background:#fff3cd;padding:2px 8px;border-radius:99px}}
</style></head><body><main>
<span class="badge">Emulator - no real credentials</span>
<h1>Sign in to {html.escape(provider_title)}</h1>
<p><strong>{html.escape(app_name)}</strong> is requesting access to:</p>
<ul>{scope_items}</ul>
<p>Choose an account to continue:</p>
<ul class="accounts">{items}</ul>
</main></body></html>"""
