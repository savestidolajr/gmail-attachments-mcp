# Multi-user hosted MCP with OAuth: design

Date: 2026-10-06
Status: draft, awaiting review

## Goal
Let other people use the hosted gmail-attachments MCP with their own Gmail. A user gets a connector URL, signs in with Google once, and then any MCP client (Claude web, mobile, Desktop, Code) can list and read their own attachments. No cross-user access. The owner's current setup keeps working.

## Scope
- **Stage A (this spec):** up to 100 hand-picked users, Google OAuth app in "Testing" mode, email allowlist.
- **Stage B (later, not built here):** public service. Needs Google restricted-scope verification, an annual third-party security assessment (CASA), privacy policy, and legal review. Stage A is designed so B adds paperwork and an optional allowlist, not rework.

Out of scope: write access to Gmail (stays `gmail.readonly`), a `download_attachment` tool on the hosted server (no disk), billing, an admin UI (allowlist is edited directly in the database).

## Current state
- `serverless.py`: stateless per-request ASGI app on Vercel, one static bearer token (`MCP_AUTH_TOKEN`).
- `server.py`: tools build the Gmail client through `_service()`, which reads one account's credentials from env vars (`GMAIL_ATT_REFRESH_TOKEN` etc.).
- Vercel functions keep no state, so per-user tokens and OAuth state need a database.

## Decisions
- **Storage:** Neon Postgres via the Vercel Marketplace.
- **Auth model:** the server is its own OAuth 2.1 authorization server (PKCE S256, dynamic client registration) and delegates user login to Google. Chosen over a hosted identity provider (extra vendor, awkward at stage B) and per-user bearer tokens (do not work on Claude web or mobile).
- **Owner fallback:** the static `MCP_AUTH_TOKEN` maps to the owner user.

## Components
| Module | Responsibility |
|---|---|
| `store.py` | All Postgres access. No raw SQL elsewhere. |
| `crypto.py` | AES-GCM encrypt/decrypt of Google refresh tokens, key from `TOKEN_ENC_KEY`. |
| `oauth.py` | OAuth server endpoints: discovery metadata, `/register`, `/authorize`, `/google/callback`, `/token`, `/revoke`. Built on the MCP Python SDK's OAuth server support where it fits. |
| `serverless.py` | Entry point. Routes OAuth and `/mcp`, resolves caller to a user, passes identity to tools. |
| `server.py` | `_service()` takes a user and builds the Gmail client from that user's stored token. Tool bodies unchanged. |

## Flow
1. Client calls `/mcp`, gets 401 with discovery metadata pointing at this server's OAuth endpoints.
2. Client registers, starts `/authorize` with PKCE.
3. Server redirects to Google (`gmail.readonly` plus email).
4. Google calls back. Server checks the email against the allowlist, stores the encrypted refresh token, issues an auth code.
5. Client exchanges the code for an MCP access token (1 hour) and refresh token (30 days, rotated on use).
6. Each `/mcp` call: validate token, load user, load their Google token, call Gmail as them.

## Data model (Neon Postgres)
- `users(id, email, google_refresh_token_enc, created_at, revoked_at)`
- `allowlist(email, added_at)`
- `oauth_clients(client_id, redirect_uris, name, created_at)`
- `auth_codes(code_hash, client_id, user_id, pkce_challenge, expires_at, used_at)`: 5 minute life, single use
- `mcp_tokens(token_hash, user_id, client_id, kind, expires_at, revoked_at)`: kind is access or refresh
- `audit_log(ts, user_id, tool, message_id, ok)`: calls and message IDs only, never attachment content

## Security
- MCP tokens and auth codes stored as hashes. Google refresh tokens AES-GCM encrypted; `TOKEN_ENC_KEY` lives in Vercel env, never in the database.
- Redirect URIs must exactly match registration. PKCE S256 required. OAuth `state` verified on the Google callback.
- `user_id` comes only from the validated MCP token, never from tool arguments. Every Gmail call uses the caller's own token.
- Per-user rate limit (about 60 calls per minute). Existing 1.5 MB cap on `get_attachment_base64` stays.
- `/revoke` lets a user disconnect: revokes the Google token and deletes the stored copy.
- Testing mode expires Google refresh tokens after 7 days. A dead token returns a clear "reconnect" error and the user re-runs sign-in. Known stage A limitation.

## Errors
- Missing or invalid token: 401 with OAuth discovery header.
- Email not on allowlist: plain "not approved" page.
- Dead Google token: tool error telling the user to reconnect.

## Testing
- Unit: crypto round trip, PKCE verification, token hashing and expiry, allowlist check.
- Integration with a fake Google (no network): full authorise, token, tool-call flow, plus a cross-user isolation test (user A's token can never read user B's mail).
- Manual: one real second Gmail account connected from Claude web and mobile.

## Stage B hooks (built now, used later)
Allowlist is optional, audit log and revoke already exist. Remaining B work is non-code: Google verification, CASA assessment, privacy policy, legal advice.

## Open items
- Exact SDK OAuth hooks to reuse versus hand-roll, decided during planning after reading the installed `mcp` version.
- Neon setup steps and env var names, settled in the plan.
