"""Who is calling. The hosted dispatcher sets `current_user` from the validated MCP token;
tools read it. It is never taken from tool arguments."""
from contextvars import ContextVar

OWNER = "owner"  # the static MCP_AUTH_TOKEN user, and local (stdio) mode

current_user: ContextVar[str | None] = ContextVar("current_user", default=None)


class NotConnected(RuntimeError):
    """The caller's Google connection is missing, expired or revoked."""
