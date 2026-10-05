"""Serverless entry point (Vercel). No lifespan events: the MCP session manager is
started per request, in stateless JSON mode, so cold starts and short-lived
instances behave."""
import hmac
import os

os.environ["GMAIL_ATT_REMOTE"] = "1"

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse  # noqa: E402

from .server import mcp  # noqa: E402

REQUIRED = ("MCP_AUTH_TOKEN", "GMAIL_ATT_CLIENT_ID", "GMAIL_ATT_CLIENT_SECRET", "GMAIL_ATT_REFRESH_TOKEN")


def _authorised(scope) -> bool:
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    if len(token) < 32:
        return False  # fail closed
    given = dict(scope["headers"]).get(b"authorization", b"")
    return given.lower().startswith(b"bearer ") and hmac.compare_digest(
        given[7:].strip(), token.encode()
    )


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    path = scope["path"]
    if path == "/health":
        await PlainTextResponse("ok")(scope, receive, send)
        return
    if not _authorised(scope):
        await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        return
    missing = [v for v in REQUIRED if not os.environ.get(v)]
    if missing:
        await JSONResponse({"error": f"server misconfigured: {', '.join(missing)} not set"}, status_code=500)(
            scope, receive, send
        )
        return
    if path.rstrip("/") != "/mcp":
        await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)
        return
    manager = StreamableHTTPSessionManager(app=mcp._mcp_server, stateless=True, json_response=True)
    async with manager.run():
        await manager.handle_request(scope, receive, send)
