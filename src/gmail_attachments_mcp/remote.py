"""Hosted entry point: streamable-HTTP MCP behind a bearer token (for Railway etc.)."""
import hmac
import os
import sys

# Must be set before server.py is imported.
os.environ["GMAIL_ATT_REMOTE"] = "1"

import uvicorn  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse  # noqa: E402

from .server import mcp  # noqa: E402

OPEN_PATHS = {"/health"}


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request):
    return PlainTextResponse("ok")


class BearerAuth:
    """Pure ASGI middleware (safe with streaming responses)."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] not in OPEN_PATHS:
            headers = dict(scope["headers"])
            given = headers.get(b"authorization", b"")
            if not given.lower().startswith(b"bearer ") or not hmac.compare_digest(
                given[7:].strip(), self.token
            ):
                resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app():
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    if len(token) < 32:
        sys.exit("MCP_AUTH_TOKEN must be set to a random string of 32+ characters.")
    for var in ("GMAIL_ATT_CLIENT_ID", "GMAIL_ATT_CLIENT_SECRET", "GMAIL_ATT_REFRESH_TOKEN"):
        if not os.environ.get(var):
            sys.exit(f"{var} is not set. Run gmail-attachments-export locally to get the values.")
    return BearerAuth(mcp.streamable_http_app(), token)


def main() -> None:
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
