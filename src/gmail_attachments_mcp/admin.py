"""Admin commands for the hosted multi-user server. Both need DATABASE_URL in the environment."""
import sys

from .store import get_store


def initdb() -> None:
    get_store().init_schema()
    print("Schema ready.")


def allow() -> None:
    emails = sys.argv[1:]
    if not emails:
        print("usage: gmail-attachments-allow EMAIL [EMAIL ...]", file=sys.stderr)
        sys.exit(2)
    store = get_store()
    for email in emails:
        store.add_allowed(email)
    print(f"Allowlisted {len(emails)} address(es).")
