"""OAuth 2.1 authorization server for the hosted MCP. The MCP SDK supplies the endpoint
handlers (register, authorize, token, revoke, metadata); this module supplies the provider
behind them, Google sign-in, and the /google/callback leg."""
import asyncio
import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from .crypto import decrypt, encrypt, hash_token, new_token, sign_state, verify_state

logger = logging.getLogger(__name__)

MCP_SCOPE = "gmail.readonly"
GOOGLE_SCOPES = "openid email https://www.googleapis.com/auth/gmail.readonly"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"


FIXED_REDIRECT_URIS = frozenset({
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def redirect_uri_allowed(uri: str) -> bool:
    """Registration is open, so only callbacks we trust may be registered: the Claude
    callbacks, loopback http (native/CLI clients), and operator-listed exact URIs."""
    extra = {u.strip() for u in os.environ.get("MCP_ALLOWED_REDIRECT_URIS", "").split(",") if u.strip()}
    if uri in FIXED_REDIRECT_URIS or uri in extra:
        return True
    try:
        parts = urlsplit(uri)
        return (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS
                and parts.username is None and parts.password is None)
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    base_url: str
    google_client_id: str
    google_client_secret: str

    @property
    def callback_url(self) -> str:
        return f"{self.base_url}/google/callback"

    @property
    def resource_url(self) -> str:
        return f"{self.base_url}/mcp"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            base_url=os.environ["PUBLIC_BASE_URL"].rstrip("/"),
            google_client_id=os.environ["GOOGLE_WEB_CLIENT_ID"],
            google_client_secret=os.environ["GOOGLE_WEB_CLIENT_SECRET"],
        )


class GoogleLogin:
    """The only code that talks to Google's OAuth endpoints."""

    def __init__(self, settings: Settings):
        self._s = settings

    def authorization_url(self, state: str) -> str:
        return GOOGLE_AUTH_URL + "?" + urlencode({
            "client_id": self._s.google_client_id,
            "redirect_uri": self._s.callback_url,
            "response_type": "code",
            "scope": GOOGLE_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        })

    def exchange_code(self, code: str) -> dict:
        resp = httpx.post(GOOGLE_TOKEN_URL, timeout=15, data={
            "code": code,
            "client_id": self._s.google_client_id,
            "client_secret": self._s.google_client_secret,
            "redirect_uri": self._s.callback_url,
            "grant_type": "authorization_code",
        })
        resp.raise_for_status()
        return resp.json()

    def email_for(self, access_token: str) -> tuple[str, bool]:
        resp = httpx.get(GOOGLE_USERINFO_URL, timeout=15,
                         headers={"Authorization": f"Bearer {access_token}"})
        resp.raise_for_status()
        data = resp.json()
        return data["email"].lower(), bool(data.get("email_verified"))

    def revoke(self, refresh_token: str) -> None:
        try:
            httpx.post(GOOGLE_REVOKE_URL, timeout=15, data={"token": refresh_token})
        except httpx.HTTPError:
            pass  # best effort; the stored copy is wiped regardless


async def _t(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


class GmailOAuthProvider:
    def __init__(self, store, google, settings: Settings):
        self.store, self.google, self._settings = store, google, settings

    async def get_client(self, client_id: str):
        raw = await _t(self.store.get_client, client_id)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not redirect_uri_allowed(str(uri)):
                raise RegistrationError(
                    "invalid_redirect_uri", "redirect_uri is not allowed for this server"
                )
        await _t(self.store.save_client, client_info.client_id, client_info.model_dump_json())

    async def authorize(self, client, params: AuthorizationParams) -> str:
        state = sign_state({
            "cid": client.client_id,
            "ru": str(params.redirect_uri),
            "rue": params.redirect_uri_provided_explicitly,
            "cc": params.code_challenge,
            "st": params.state,
            "sc": params.scopes or [MCP_SCOPE],
            "res": params.resource,
        })
        return self.google.authorization_url(state)

    async def load_authorization_code(self, client, authorization_code: str):
        row = await _t(self.store.get_auth_code, hash_token(authorization_code))
        if not row or row["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=row["scopes"],
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
            resource=row["resource"],
            subject=row["user_id"],
        )

    async def exchange_authorization_code(self, client, authorization_code) -> OAuthToken:
        if not await _t(self.store.consume_auth_code, hash_token(authorization_code.code)):
            raise TokenError("invalid_grant", "authorization code already used or expired")
        return await self._issue(
            authorization_code.subject, client.client_id, authorization_code.scopes,
            authorization_code.resource,
        )

    async def load_refresh_token(self, client, refresh_token: str):
        row = await _t(self.store.get_token, hash_token(refresh_token), "refresh")
        if not row or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token, client_id=row["client_id"], scopes=row["scopes"],
            expires_at=int(row["expires_at"]), resource=row["resource"], subject=row["user_id"],
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes) -> OAuthToken:
        if not await _t(self.store.revoke_token, hash_token(refresh_token.token)):
            raise TokenError("invalid_grant", "refresh token already used or revoked")
        return await self._issue(
            refresh_token.subject, client.client_id, scopes or refresh_token.scopes,
            refresh_token.resource,
        )

    async def load_access_token(self, token: str):
        row = await _t(self.store.get_token, hash_token(token), "access")
        if not row:
            return None
        return AccessToken(
            token=token, client_id=row["client_id"], scopes=row["scopes"],
            expires_at=int(row["expires_at"]), resource=row["resource"], subject=row["user_id"],
        )

    async def revoke_token(self, token) -> None:
        """Revoking any MCP token disconnects the user: Google token revoked and wiped, all
        MCP tokens revoked. A user signs in again to reconnect."""
        if token.subject:
            await self.disconnect(token.subject)

    async def disconnect(self, user_id: str) -> None:
        user = await _t(self.store.get_user, user_id)
        try:
            if user and user["refresh_token_enc"]:
                refresh = decrypt(user["refresh_token_enc"], user["email"])
                await _t(self.google.revoke, refresh)
        except Exception:
            logger.exception("google revoke failed")
        finally:
            # The local wipe must happen in every case.
            await _t(self.store.disconnect_user, user_id)

    async def _issue(self, user_id, client_id, scopes, resource) -> OAuthToken:
        access, refresh, now = new_token(), new_token(), time.time()
        for token, kind, ttl in ((access, "access", ACCESS_TTL), (refresh, "refresh", REFRESH_TTL)):
            await _t(
                self.store.save_token, hash_token(token),
                user_id=user_id, client_id=client_id, kind=kind, scopes=scopes,
                resource=resource, expires_at=now + ttl,
            )
        return OAuthToken(
            access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
            scope=" ".join(scopes), refresh_token=refresh,
        )


def _page(message: str, status: int) -> HTMLResponse:
    html = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>Gmail attachments</title><body style='font-family:sans-serif;max-width:32rem;"
        f"margin:3rem auto;padding:0 1rem'><p>{message}</p></body>"
    )
    return HTMLResponse(html, status_code=status)


def build_routes(provider: GmailOAuthProvider, settings: Settings) -> list[Route]:
    routes = create_auth_routes(
        provider,
        issuer_url=AnyHttpUrl(settings.base_url),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[MCP_SCOPE], default_scopes=[MCP_SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    routes += create_protected_resource_routes(
        resource_url=AnyHttpUrl(settings.resource_url),
        authorization_servers=[AnyHttpUrl(settings.base_url)],
        scopes_supported=[MCP_SCOPE],
        resource_name="Gmail attachments",
    )

    async def google_callback(request):
        params = request.query_params
        payload = verify_state(params.get("state", ""))
        if payload is None:
            return _page("This sign-in link is invalid or has expired. Start again from your app.", 400)
        if params.get("error"):
            return RedirectResponse(
                construct_redirect_uri(payload["ru"], error="access_denied", state=payload["st"]), 302
            )
        code = params.get("code")
        if not code:
            return _page("Google did not return a sign-in code. Start again from your app.", 400)
        try:
            tokens = await _t(provider.google.exchange_code, code)
            email, verified = await _t(provider.google.email_for, tokens["access_token"])
        except Exception as exc:
            # Class name and Google's status/error code only: never the body, tokens, code, email.
            detail = ""
            if isinstance(exc, httpx.HTTPStatusError):
                detail = f" status={exc.response.status_code}"
                try:
                    err = exc.response.json().get("error")
                    if isinstance(err, str):
                        detail += f" error={err[:64]}"
                except Exception:
                    pass
            logger.error("google sign-in exchange failed: %s%s", type(exc).__name__, detail)
            return _page("Could not complete sign-in with Google. Try again in a moment.", 502)
        email = email.lower()
        if not verified or not await _t(provider.store.is_allowed, email):
            return _page("This Google account is not approved for this service.", 403)
        refresh = tokens.get("refresh_token")
        if not refresh:
            return _page(
                "Google did not grant offline access. Remove this app at "
                "myaccount.google.com/permissions, then try again.", 400,
            )
        user_id = await _t(provider.store.upsert_user, email, encrypt(refresh, email))
        auth_code = new_token()
        await _t(
            provider.store.save_auth_code, hash_token(auth_code),
            client_id=payload["cid"], user_id=user_id, code_challenge=payload["cc"],
            redirect_uri=payload["ru"], redirect_uri_provided_explicitly=payload["rue"],
            resource=payload["res"], scopes=payload["sc"], expires_at=time.time() + CODE_TTL,
        )
        return RedirectResponse(
            construct_redirect_uri(payload["ru"], code=auth_code, state=payload["st"]), 302
        )

    routes.append(Route("/google/callback", google_callback, methods=["GET"]))
    return routes
