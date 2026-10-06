"""Google credentials for a given user."""
import os

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .auth import SCOPES, get_credentials
from .crypto import decrypt
from .identity import OWNER, NotConnected
from .store import get_store

RECONNECT = (
    "Your Google connection expired or was revoked. Reconnect the connector to sign in again."
)


def _refresh(creds: Credentials) -> None:
    creds.refresh(Request())


def credentials_for(user_id: str) -> Credentials:
    if user_id == OWNER:
        return get_credentials()
    store = get_store()
    user = store.get_user(user_id)
    if not user or user["revoked_at"] is not None or not user["refresh_token_enc"]:
        raise NotConnected(RECONNECT)
    creds = Credentials(
        token=None,
        refresh_token=decrypt(user["refresh_token_enc"], user["email"]),
        client_id=os.environ["GOOGLE_WEB_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_WEB_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    try:
        _refresh(creds)
    except RefreshError:
        store.disconnect_user(user_id)  # next /mcp call gets 401 and the client re-runs sign-in
        raise NotConnected(RECONNECT)
    return creds
