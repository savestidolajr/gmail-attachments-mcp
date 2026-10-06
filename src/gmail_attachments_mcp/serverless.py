"""Serverless entry point (Vercel). Dispatches /mcp (bearer-authenticated) and the OAuth
endpoints. No lifespan events: the MCP session manager is started per request, in stateless
JSON mode, so cold starts and short-lived instances behave."""
import hmac
import os

os.environ["GMAIL_ATT_REMOTE"] = "1"

from mcp.server.auth.routes import build_resource_metadata_url  # noqa: E402
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: E402
from pydantic import AnyHttpUrl  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse  # noqa: E402

from .identity import OWNER, current_user  # noqa: E402
from .oauth import GmailOAuthProvider, GoogleLogin, Settings, build_routes  # noqa: E402
from .server import mcp  # noqa: E402
from .store import get_store  # noqa: E402

REQUIRED = (
    "DATABASE_URL",
    "TOKEN_ENC_KEY",
    "PUBLIC_BASE_URL",
    "GOOGLE_WEB_CLIENT_ID",
    "GOOGLE_WEB_CLIENT_SECRET",
)


def _bearer(scope) -> str:
    given = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
    return given[7:].strip() if given.lower().startswith("bearer ") else ""


def _is_owner_token(token: str) -> bool:
    static = os.environ.get("MCP_AUTH_TOKEN", "")
    return len(static) >= 32 and bool(token) and hmac.compare_digest(token.encode(), static.encode())


def _make_mcp_handler(resolve_user, unauthorized):
    """The /mcp leg shared by both modes: resolve the caller, then run a per-request stateless
    manager with `current_user` set (and reset)."""

    async def handle_mcp(scope, receive, send):
        token = _bearer(scope)
        user_id = await resolve_user(token)
        if not user_id:  # None or "": never run a tool without a real identity
            await unauthorized(bool(token))(scope, receive, send)
            return
        marker = current_user.set(user_id)
        try:
            manager = StreamableHTTPSessionManager(app=mcp._mcp_server, stateless=True, json_response=True)
            async with manager.run():
                await manager.handle_request(scope, receive, send)
        finally:
            current_user.reset(marker)

    return handle_mcp


def _dispatcher(handle_mcp, fallback):
    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope["path"]
        if path == "/health":
            await PlainTextResponse("ok")(scope, receive, send)
        elif path.rstrip("/") == "/mcp":
            await handle_mcp(scope, receive, send)
        else:
            await fallback(scope, receive, send)

    return asgi


def build_owner_app():
    """Owner-only mode (no DATABASE_URL): the static MCP_AUTH_TOKEN is the only credential
    and there are no OAuth routes."""

    async def resolve_user(token: str):
        return OWNER if _is_owner_token(token) else None

    def unauthorized(had_token: bool) -> JSONResponse:
        return JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})

    async def not_found(scope, receive, send):
        await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)

    return _dispatcher(_make_mcp_handler(resolve_user, unauthorized), not_found)


def build_app(store, google, settings: Settings):
    provider = GmailOAuthProvider(store, google, settings)
    oauth_app = Starlette(routes=build_routes(provider, settings))
    metadata_url = str(build_resource_metadata_url(AnyHttpUrl(settings.resource_url)))

    def unauthorized(had_token: bool) -> JSONResponse:
        challenge = f'Bearer resource_metadata="{metadata_url}"'
        if had_token:
            challenge = 'Bearer error="invalid_token", ' + challenge[len("Bearer "):]
        return JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": challenge})

    async def resolve_user(token: str):
        if _is_owner_token(token):
            return OWNER
        if not token:
            return None
        access = await provider.load_access_token(token)
        return access.subject if access else None

    return _dispatcher(_make_mcp_handler(resolve_user, unauthorized), oauth_app)


_app = None


async def app(scope, receive, send):
    global _app
    if scope["type"] != "http":
        return
    if scope["path"] == "/health":
        await PlainTextResponse("ok")(scope, receive, send)
        return
    if _app is None:
        if not os.environ.get("DATABASE_URL"):
            _app = build_owner_app()  # single-user hosted deployment: static token only
        else:
            missing = [v for v in REQUIRED if not os.environ.get(v)]
            if missing:
                await JSONResponse({"error": f"server misconfigured: {', '.join(missing)} not set"},
                                   status_code=500)(scope, receive, send)
                return
            settings = Settings.from_env()
            _app = build_app(get_store(), GoogleLogin(settings), settings)
    await _app(scope, receive, send)
