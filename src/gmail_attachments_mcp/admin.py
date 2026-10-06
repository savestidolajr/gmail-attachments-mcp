"""Admin commands for the hosted multi-user server. Both need DATABASE_URL in the environment."""
import sys

from .store import get_store


def _store():
    try:
        return get_store()
    except KeyError:
        print(
            "DATABASE_URL is not set. Export the Neon connection string in this shell first "
            "(see the README, multi-user setup step 4).",
            file=sys.stderr,
        )
        sys.exit(2)


def initdb() -> None:
    _store().init_schema()
    print("Schema ready.")


def allow() -> None:
    emails = sys.argv[1:]
    if not emails:
        print("usage: gmail-attachments-allow EMAIL [EMAIL ...]", file=sys.stderr)
        sys.exit(2)
    store = _store()
    for email in emails:
        store.add_allowed(email)
    print(f"Allowlisted {len(emails)} address(es).")
