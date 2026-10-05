"""OAuth for the Gmail API. Read-only scope. Secrets live outside the repo."""
import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CONFIG_DIR = Path(
    os.environ.get("GMAIL_ATT_CONFIG_DIR", Path.home() / ".config" / "gmail-attachments-mcp")
)
CLIENT_FILE = Path(os.environ.get("GMAIL_ATT_CLIENT_FILE", CONFIG_DIR / "credentials.json"))
TOKEN_FILE = CONFIG_DIR / "token.json"


def _save(creds: Credentials) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)


def get_credentials(interactive: bool = False) -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save(creds)
        return creds
    if not interactive:
        raise RuntimeError(
            "Not authorised. Run `uv run gmail-attachments-auth` once in a terminal."
        )
    if not CLIENT_FILE.exists():
        raise RuntimeError(f"OAuth client file not found: {CLIENT_FILE}")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    _save(creds)
    return creds


def main() -> None:
    try:
        get_credentials(interactive=True)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    print(f"Authorised. Token saved to {TOKEN_FILE}")
