# gmail-attachments-mcp

Local MCP server that fills the gap in the hosted Gmail connector: it can't download attachments. This one can. Read-only (`gmail.readonly`).

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

## Register with Claude Code
```
git clone https://github.com/savestidolajr/gmail-attachments-mcp
claude mcp add gmail-attachments --scope user -- uv run --directory /absolute/path/to/gmail-attachments-mcp gmail-attachments-mcp
```

## Env vars
- `GMAIL_ATT_CONFIG_DIR`: where credentials/token live
- `GMAIL_ATT_CLIENT_FILE`: path to the OAuth client JSON
- `GMAIL_ATT_DOWNLOAD_DIR`: default download folder

## Notes
- While the OAuth app is in "Testing", Google expires the refresh token after 7 days. Re-run the auth command, or publish the app to "In production" (no verification needed for personal use of a single account).
- Never commit `credentials.json` or `token.json`. `.gitignore` covers both.

## Privacy
Runs locally. Uses only the read-only Gmail scope. Your OAuth client and token stay on your machine in `~/.config/gmail-attachments-mcp/`. Each user must create their own Google Cloud OAuth client.

## Licence
MIT
