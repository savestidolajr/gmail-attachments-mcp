# gmail-attachments-mcp

**Give your AI agent the ability to open Gmail attachments.**

Most Gmail integrations let an agent search and read email bodies but stop at the attachment. This MCP server fills that gap. Your agent can list what is attached to a message, read PDFs, Word, Excel and PowerPoint files straight into its context, or save any attachment to disk. Read-only (`gmail.readonly`), runs locally on your machine.

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

## Host it on Railway (use from any device)

The local server only works on the machine it runs on. To reach it from other devices, run the hosted mode: the same tools over streamable HTTP, protected by a bearer token.

Differences from local mode: no `download_attachment` (no persistent disk), and credentials come from env vars instead of `token.json`.

1. Do the one-time Google setup and `uv run gmail-attachments-auth` locally first.
2. Print the three secrets Railway needs (output is secret, do not paste it anywhere public):
   ```
   uv run gmail-attachments-export
   ```
3. Generate a random access token:
   ```
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
4. In Railway: **New Project, Deploy from GitHub repo**, pick this repo. Railway builds the `Dockerfile`.
5. Add variables: `GMAIL_ATT_CLIENT_ID`, `GMAIL_ATT_CLIENT_SECRET`, `GMAIL_ATT_REFRESH_TOKEN`, `MCP_AUTH_TOKEN`.
6. Under **Settings, Networking**, generate a public domain.
7. Connect your client to `https://<your-domain>/mcp` with header `Authorization: Bearer <MCP_AUTH_TOKEN>`. For Claude Code:
   ```
   claude mcp add --transport http gmail-attachments https://<your-domain>/mcp --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
   ```

Security: anyone with the bearer token can read the attachments in the Gmail account. Keep the token secret, keep the scope read-only, and rotate `MCP_AUTH_TOKEN` if it leaks. Host it for yourself only; do not run it as a shared service holding other people's Gmail tokens. The server refuses to start without a token of 32+ characters. `/health` is the only unauthenticated route.

Note: claude.ai web custom connectors expect OAuth, not a static bearer token, so this setup targets Claude Code, Claude Desktop and other clients that accept custom headers.

## Env vars
- `GMAIL_ATT_CONFIG_DIR`: where credentials/token live
- `GMAIL_ATT_CLIENT_FILE`: path to the OAuth client JSON
- `GMAIL_ATT_DOWNLOAD_DIR`: default download folder
- Hosted mode only: `GMAIL_ATT_CLIENT_ID`, `GMAIL_ATT_CLIENT_SECRET`, `GMAIL_ATT_REFRESH_TOKEN`, `MCP_AUTH_TOKEN`, `PORT` (set by Railway)

## Notes
- While the OAuth app is in "Testing", Google expires the refresh token after 7 days. Re-run the auth command, or publish the app to "In production" (no verification needed for personal use of a single account).
- Never commit `credentials.json` or `token.json`. `.gitignore` covers both.

## Privacy
Runs locally. Uses only the read-only Gmail scope. Your OAuth client and token stay on your machine in `~/.config/gmail-attachments-mcp/`. Each user must create their own Google Cloud OAuth client.

## Licence
MIT
