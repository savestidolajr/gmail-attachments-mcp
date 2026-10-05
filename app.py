"""Vercel entrypoint: exposes the ASGI `app`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from gmail_attachments_mcp.serverless import app  # noqa: E402,F401
