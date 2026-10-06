# gmail-attachments-mcp

**Give your AI agent the ability to open Gmail attachments.**

Most Gmail integrations let an agent search and read email bodies but stop at the attachment. This MCP server fills that gap. Your agent can list what is attached to a message, read PDFs, Word, Excel and PowerPoint files straight into its context, or save any attachment to disk. Read-only (`gmail.readonly`). Runs locally on your machine, or hosted on Vercel for yourself or a small allowlist of people.

Works with any MCP client: Claude Code, Claude Desktop, Cursor, and others.

## What your agent can do with it
- "Summarise the PDF the recruiter sent me."
- "Read the invoice attached to that Anthropic email and check the totals match the email body."
- "Pull the job description from the latest email and compare it to my CV."
- "Download the spreadsheet from Sam's email and total column C."
- "List every attachment in that thread."

Pair it with a Gmail connector: the connector finds the message and gives the `message_id`, this server opens the attachments.

## Tools
- `list_attachments(message_id)`: name, type, size
- `download_attachment(message_id, filename, dest_dir="", index=0)`: saves to `~/Downloads/gmail-attachments` by default, never overwrites
- `get_attachment_base64(message_id, filename, index=0)`: returns raw file bytes as base64 so a client can write them to disk (works on the hosted server; refuses files over ~1.5 MB)
- `read_attachment_text(message_id, filename, index=0, max_chars=20000)`: extracts text without saving the file. Supported types:
  - PDF (text-based; scanned PDFs return a hint to download instead)
  - Word `.docx` (paragraphs and tables)
  - Excel `.xlsx` (every sheet, rows joined with ` | `)
  - PowerPoint `.pptx` (text per slide)
  - Plain text: `.txt`, `.csv`, `.json`, `.xml`, `.html`, `.md`
  - Anything else (images, zip, legacy `.doc` / `.xls` / `.ppt`): use `download_attachment`, then open the file yourself.

`message_id` is the same id the Gmail connector returns from `search_threads` / `get_message`.

## One-time setup
1. Google Cloud Console: create a project, enable the **Gmail API**.
2. OAuth consent screen: External, add your Gmail as a test user.
3. Credentials: create an **OAuth client ID**, type **Desktop app**. Download the JSON.
4. Save it as `~/.config/gmail-attachments-mcp/credentials.json` (outside the repo).
5. Authorise once (opens a browser):
   ```
   uv run gmail-attachments-auth
   ```
   Token is saved to `~/.config/gmail-attachments-mcp/token.json` (mode 600).

## Install in your agent

Clone the repo first (needs [uv](https://docs.astral.sh/uv/)):
```
git clone https://github.com/savestidolajr/gmail-attachments-mcp
```

**Claude Code**
```
claude mcp add gmail-attachments --scope user -- uv run --directory /absolute/path/to/gmail-attachments-mcp gmail-attachments-mcp
```

**Claude Desktop, Cursor and other MCP clients**: add this to the client's MCP config (`claude_desktop_config.json` for Claude Desktop):
```json
{
  "mcpServers": {
    "gmail-attachments": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/gmail-attachments-mcp", "gmail-attachments-mcp"]
    }
  }
}
```
Restart the client, then ask it to list the attachments on any email.

## Host it on Vercel (use from any device)

The local server only works on the machine it runs on. Hosted mode runs the same tools over streamable HTTP, protected by a bearer token, as a Vercel Python service. It has no `download_attachment` (no persistent disk) and takes credentials from env vars instead of `token.json`. `app.py` exposes the ASGI app, `vercel.json` declares it as a service and routes everything to it. The MCP session runs per request (stateless, JSON responses), so it does not depend on lifespan events.

1. Do the one-time Google setup and `uv run gmail-attachments-auth` locally.
2. `npm i -g vercel`, then `vercel login`.
3. `./scripts/vercel-setup.sh` links the project and sets the four env vars from your local OAuth files, piped straight to Vercel. It prints the generated `MCP_AUTH_TOKEN` once. Save it.
4. `vercel deploy --prod`
5. Connect your client to `https://<your-project>.vercel.app/mcp`. For Claude Code:
   ```
   claude mcp add --transport http gmail-attachments https://<your-project>.vercel.app/mcp --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
   ```

### Which clients can connect to the hosted server

The hosted server only accepts `Authorization: Bearer <MCP_AUTH_TOKEN>`, so the client must be able to send a custom header.

| Client | Works? |
|---|---|
| Claude Code (any machine) | Yes, using `--header` as above |
| Claude Desktop, Cursor, other config-file clients | Likely, if they support HTTP MCP servers with headers (otherwise bridge with `mcp-remote`) |
| Claude.ai web and mobile (custom connectors) | Yes, in multi-user mode (OAuth). Not with the single static token. |

To add it on another machine, run the `claude mcp add --transport http ...` command above with the same token.

In single-user mode the server reads one Gmail account: whoever owns the refresh token in its env vars. In multi-user mode (below) each person signs in with their own Google account and only ever sees their own mail.

To rotate the token later and re-register it in Claude Code in one step, with the token never printed: `./scripts/rotate-and-register.sh`.

Caveats: the default function time limit applies to large attachments, cold starts import the Google and Office libraries, and Vercel's deployment protection may block the URL until you disable it for this project. Security: anyone with the bearer token can read attachments in the Gmail account. Keep the token secret, keep the scope read-only, rotate `MCP_AUTH_TOKEN` if it leaks, and host it for yourself only, never as a shared service holding other people's Gmail tokens. Without a token of 32+ characters the server rejects every request. `/health` is the only unauthenticated route.

## Multi-user hosted mode (allowlist)

The hosted server is also an OAuth 2.1 server. Someone you approve adds your connector URL, signs in with Google once, and then their MCP client can list and read **their own** attachments. Works in Claude web, mobile, Desktop and Code.

How it works: the MCP SDK provides the OAuth endpoints; the server stores each user's Google refresh token encrypted (AES-GCM) in Postgres, issues its own short-lived MCP tokens (1 hour access, 30 day rotating refresh, stored as hashes), and checks the signed-in Google email against an allowlist. Every Gmail call uses the caller's own token. Your static `MCP_AUTH_TOKEN` still works as the owner.

Limits to know about:
- While your Google OAuth app is in "Testing", Google expires each user's refresh token after 7 days. They get a "reconnect" error, then sign in again. Up to 100 test users.
- You hold access to every approved user's mail. Only approve people you trust, keep the database and `TOKEN_ENC_KEY` safe, and tell users they can disconnect any time (revoking the connector wipes their stored token).
- Public MCP clients that cannot send a client secret may be unable to call the `/revoke` endpoint, so disconnecting is most reliable through the operator SQL below or by removing the app in Google permissions.
- Making this public for anyone needs Google restricted-scope verification and an annual third-party security assessment. Not done.

Offboarding someone:
- Removing an email from the allowlist only blocks new sign-ins. To cut off someone who is already connected, disconnect them too. Run this in the Neon SQL editor or psql, replacing `friend@gmail.com`:
  ```sql
  UPDATE mcp_tokens SET revoked_at = extract(epoch from now())
    WHERE user_id = (SELECT id FROM users WHERE email = 'friend@gmail.com') AND revoked_at IS NULL;
  UPDATE users SET google_refresh_token_enc = NULL, revoked_at = extract(epoch from now())
    WHERE email = 'friend@gmail.com';
  DELETE FROM allowlist WHERE email = 'friend@gmail.com';
  ```
- They can also remove the app at myaccount.google.com/permissions to revoke Google's side.

Setup (once):
1. **Postgres:** Vercel dashboard, Storage, create a Neon database and connect it to the project. This adds `DATABASE_URL`.
2. **Google OAuth client:** in Google Cloud, create an OAuth client of type **Web application** with redirect URI `https://<your-project>.vercel.app/google/callback`. Keep the consent screen on Testing and add each user's Gmail as a test user.
3. **Env vars** (Vercel, production): `PUBLIC_BASE_URL` (`https://<your-project>.vercel.app`, no trailing slash), `GOOGLE_WEB_CLIENT_ID`, `GOOGLE_WEB_CLIENT_SECRET`, `TOKEN_ENC_KEY`. Generate the key without printing it:
   ```
   python3 -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())" | vercel env add TOKEN_ENC_KEY production
   ```
   Losing this key means every user has to reconnect.
4. **Create tables and add people:**
   ```
   vercel env pull .env.local
   uv run --env-file .env.local gmail-attachments-initdb
   uv run --env-file .env.local gmail-attachments-allow friend@gmail.com
   ```
5. `vercel deploy --prod`. Then add `https://<your-project>.vercel.app/mcp` as a custom connector in Claude.

Run the tests with `uv run pytest`. To include the Postgres tests, start a throwaway database and set `TEST_DATABASE_URL`:
```
docker run -d --name gmail-mcp-test-pg -e POSTGRES_PASSWORD=test -p 54329:5432 postgres:16
TEST_DATABASE_URL=postgresql://postgres:test@localhost:54329/postgres uv run pytest
```

## Env vars
- `GMAIL_ATT_CONFIG_DIR`: where credentials/token live
- `GMAIL_ATT_CLIENT_FILE`: path to the OAuth client JSON
- `GMAIL_ATT_DOWNLOAD_DIR`: default download folder
- Hosted mode only: `GMAIL_ATT_CLIENT_ID`, `GMAIL_ATT_CLIENT_SECRET`, `GMAIL_ATT_REFRESH_TOKEN`, `MCP_AUTH_TOKEN` (set by `scripts/vercel-setup.sh`)
- Multi-user mode: `DATABASE_URL`, `TOKEN_ENC_KEY`, `PUBLIC_BASE_URL`, `GOOGLE_WEB_CLIENT_ID`, `GOOGLE_WEB_CLIENT_SECRET`, optional `GMAIL_ATT_RATE_LIMIT` (calls per user per minute, default 60)

## Notes
- While the OAuth app is in "Testing", Google expires the refresh token after 7 days. Re-run the auth command, or publish the app to "In production" (no verification needed for personal use of a single account).
- Never commit `credentials.json` or `token.json`. `.gitignore` covers both.

## Privacy
Runs locally. Uses only the read-only Gmail scope. Your OAuth client and token stay on your machine in `~/.config/gmail-attachments-mcp/`. Each user must create their own Google Cloud OAuth client. In multi-user hosted mode the operator's database holds each approved user's encrypted Google refresh token and an audit log of tool calls (tool name and message ID only, never attachment content).

## Licence
MIT
