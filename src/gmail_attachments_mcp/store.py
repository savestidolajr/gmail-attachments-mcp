"""Persistence for hosted multi-user mode. PgStore talks to Postgres (Neon); MemoryStore has
identical methods for tests and local experiments. All times are epoch seconds."""
import os
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id text PRIMARY KEY,
  email text UNIQUE NOT NULL,
  google_refresh_token_enc text,
  created_at double precision NOT NULL,
  revoked_at double precision
);
CREATE TABLE IF NOT EXISTS allowlist (
  email text PRIMARY KEY,
  added_at double precision NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id text PRIMARY KEY,
  info_json text NOT NULL,
  created_at double precision NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
  code_hash text PRIMARY KEY,
  client_id text NOT NULL,
  user_id text NOT NULL REFERENCES users(id),
  code_challenge text NOT NULL,
  redirect_uri text NOT NULL,
  redirect_uri_provided_explicitly boolean NOT NULL,
  resource text,
  scopes text NOT NULL,
  expires_at double precision NOT NULL,
  used_at double precision
);
CREATE TABLE IF NOT EXISTS mcp_tokens (
  token_hash text PRIMARY KEY,
  user_id text NOT NULL REFERENCES users(id),
  client_id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('access', 'refresh')),
  scopes text NOT NULL,
  resource text,
  expires_at double precision NOT NULL,
  revoked_at double precision
);
CREATE INDEX IF NOT EXISTS mcp_tokens_user ON mcp_tokens (user_id);
CREATE TABLE IF NOT EXISTS audit_log (
  id bigserial PRIMARY KEY,
  ts double precision NOT NULL,
  user_id text NOT NULL,
  tool text NOT NULL,
  message_id text,
  ok boolean NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_user_ts ON audit_log (user_id, ts);
"""

_CODE_KEYS = (
    "client_id", "user_id", "code_challenge", "redirect_uri",
    "redirect_uri_provided_explicitly", "resource", "scopes", "expires_at",
)
_TOKEN_KEYS = ("user_id", "client_id", "kind", "scopes", "resource", "expires_at")


class PgStore:
    def __init__(self, dsn: str):
        self._dsn = dsn

    def _conn(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row)

    def _one(self, sql, params=()):
        with self._conn() as conn:
            return conn.execute(sql, params).fetchone()

    def _run(self, sql, params=()) -> int:
        with self._conn() as conn:
            return conn.execute(sql, params).rowcount

    def init_schema(self) -> None:
        self._run(SCHEMA)

    def add_allowed(self, email):
        self._run(
            "INSERT INTO allowlist (email, added_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (email.lower(), time.time()),
        )

    def is_allowed(self, email):
        return self._one("SELECT 1 AS x FROM allowlist WHERE email = %s", (email.lower(),)) is not None

    def upsert_user(self, email, refresh_token_enc):
        row = self._one(
            """INSERT INTO users (id, email, google_refresh_token_enc, created_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (email) DO UPDATE
                 SET google_refresh_token_enc = EXCLUDED.google_refresh_token_enc, revoked_at = NULL
               RETURNING id""",
            (str(uuid.uuid4()), email.lower(), refresh_token_enc, time.time()),
        )
        return row["id"]

    def get_user(self, user_id):
        return self._one(
            """SELECT id, email, google_refresh_token_enc AS refresh_token_enc, revoked_at
               FROM users WHERE id = %s""",
            (user_id,),
        )

    def disconnect_user(self, user_id):
        now = time.time()
        with self._conn() as conn, conn.transaction():
            conn.execute(
                "UPDATE users SET google_refresh_token_enc = NULL, revoked_at = %s WHERE id = %s",
                (now, user_id),
            )
            conn.execute(
                "UPDATE mcp_tokens SET revoked_at = %s WHERE user_id = %s AND revoked_at IS NULL",
                (now, user_id),
            )

    def save_client(self, client_id, info_json):
        self._run(
            """INSERT INTO oauth_clients (client_id, info_json, created_at) VALUES (%s, %s, %s)
               ON CONFLICT (client_id) DO UPDATE SET info_json = EXCLUDED.info_json""",
            (client_id, info_json, time.time()),
        )

    def get_client(self, client_id):
        row = self._one("SELECT info_json FROM oauth_clients WHERE client_id = %s", (client_id,))
        return row["info_json"] if row else None

    def save_auth_code(self, code_hash, *, client_id, user_id, code_challenge, redirect_uri,
                       redirect_uri_provided_explicitly, resource, scopes, expires_at):
        self._run(
            """INSERT INTO auth_codes (code_hash, client_id, user_id, code_challenge, redirect_uri,
                 redirect_uri_provided_explicitly, resource, scopes, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (code_hash, client_id, user_id, code_challenge, redirect_uri,
             redirect_uri_provided_explicitly, resource, " ".join(scopes), expires_at),
        )

    def get_auth_code(self, code_hash):
        row = self._one(
            """SELECT client_id, user_id, code_challenge, redirect_uri, redirect_uri_provided_explicitly,
                      resource, scopes, expires_at
               FROM auth_codes WHERE code_hash = %s AND used_at IS NULL AND expires_at > %s""",
            (code_hash, time.time()),
        )
        if row:
            row["scopes"] = row["scopes"].split()
        return row

    def consume_auth_code(self, code_hash):
        return self._run(
            "UPDATE auth_codes SET used_at = %s WHERE code_hash = %s AND used_at IS NULL AND expires_at > %s",
            (time.time(), code_hash, time.time()),
        ) == 1

    def save_token(self, token_hash, *, user_id, client_id, kind, scopes, resource, expires_at):
        self._run(
            """INSERT INTO mcp_tokens (token_hash, user_id, client_id, kind, scopes, resource, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (token_hash, user_id, client_id, kind, " ".join(scopes), resource, expires_at),
        )

    def get_token(self, token_hash, kind):
        row = self._one(
            """SELECT user_id, client_id, kind, scopes, resource, expires_at
               FROM mcp_tokens
               WHERE token_hash = %s AND kind = %s AND revoked_at IS NULL AND expires_at > %s""",
            (token_hash, kind, time.time()),
        )
        if row:
            row["scopes"] = row["scopes"].split()
        return row

    def revoke_token(self, token_hash):
        return self._run(
            "UPDATE mcp_tokens SET revoked_at = %s WHERE token_hash = %s AND revoked_at IS NULL",
            (time.time(), token_hash),
        ) == 1

    def log_call(self, user_id, tool, message_id, ok):
        self._run(
            "INSERT INTO audit_log (ts, user_id, tool, message_id, ok) VALUES (%s, %s, %s, %s, %s)",
            (time.time(), user_id, tool, message_id, ok),
        )

    def count_calls(self, user_id, since_seconds):
        row = self._one(
            "SELECT count(*) AS n FROM audit_log WHERE user_id = %s AND ts > %s",
            (user_id, time.time() - since_seconds),
        )
        return row["n"]


class MemoryStore:
    def __init__(self):
        self.users, self.clients, self.codes, self.tokens, self.audit = {}, {}, {}, {}, []
        self.allowlist = set()

    def init_schema(self):
        pass

    def add_allowed(self, email):
        self.allowlist.add(email.lower())

    def is_allowed(self, email):
        return email.lower() in self.allowlist

    def upsert_user(self, email, refresh_token_enc):
        email = email.lower()
        for user in self.users.values():
            if user["email"] == email:
                user["refresh_token_enc"], user["revoked_at"] = refresh_token_enc, None
                return user["id"]
        uid = str(uuid.uuid4())
        self.users[uid] = {"id": uid, "email": email, "refresh_token_enc": refresh_token_enc, "revoked_at": None}
        return uid

    def get_user(self, user_id):
        user = self.users.get(user_id)
        return dict(user) if user else None

    def disconnect_user(self, user_id):
        now = time.time()
        if user_id in self.users:
            self.users[user_id]["refresh_token_enc"] = None
            self.users[user_id]["revoked_at"] = now
        for token in self.tokens.values():
            if token["user_id"] == user_id and token["revoked_at"] is None:
                token["revoked_at"] = now

    def save_client(self, client_id, info_json):
        self.clients[client_id] = info_json

    def get_client(self, client_id):
        return self.clients.get(client_id)

    def save_auth_code(self, code_hash, *, client_id, user_id, code_challenge, redirect_uri,
                       redirect_uri_provided_explicitly, resource, scopes, expires_at):
        self.codes[code_hash] = {
            "client_id": client_id, "user_id": user_id, "code_challenge": code_challenge,
            "redirect_uri": redirect_uri,
            "redirect_uri_provided_explicitly": redirect_uri_provided_explicitly,
            "resource": resource, "scopes": list(scopes), "expires_at": expires_at, "used_at": None,
        }

    def get_auth_code(self, code_hash):
        row = self.codes.get(code_hash)
        if not row or row["used_at"] is not None or row["expires_at"] <= time.time():
            return None
        return {k: row[k] for k in _CODE_KEYS}

    def consume_auth_code(self, code_hash):
        if self.get_auth_code(code_hash) is None:
            return False
        self.codes[code_hash]["used_at"] = time.time()
        return True

    def save_token(self, token_hash, *, user_id, client_id, kind, scopes, resource, expires_at):
        self.tokens[token_hash] = {
            "user_id": user_id, "client_id": client_id, "kind": kind, "scopes": list(scopes),
            "resource": resource, "expires_at": expires_at, "revoked_at": None,
        }

    def get_token(self, token_hash, kind):
        row = self.tokens.get(token_hash)
        if not row or row["kind"] != kind or row["revoked_at"] is not None or row["expires_at"] <= time.time():
            return None
        return {k: row[k] for k in _TOKEN_KEYS}

    def revoke_token(self, token_hash):
        row = self.tokens.get(token_hash)
        if not row or row["revoked_at"] is not None:
            return False
        row["revoked_at"] = time.time()
        return True

    def log_call(self, user_id, tool, message_id, ok):
        self.audit.append((time.time(), user_id, tool, message_id, ok))

    def count_calls(self, user_id, since_seconds):
        cutoff = time.time() - since_seconds
        return sum(1 for ts, uid, *_ in self.audit if uid == user_id and ts > cutoff)


_store = None


def get_store():
    global _store
    if _store is None:
        _store = PgStore(os.environ["DATABASE_URL"])
    return _store


def set_store(store) -> None:
    global _store
    _store = store
