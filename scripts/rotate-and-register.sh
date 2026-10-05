#!/usr/bin/env bash
# Rotate MCP_AUTH_TOKEN, redeploy to Vercel, and register the hosted server in
# Claude Code with the new token. The token is never printed or copied.
# NOTE: Claude Code stores the header in plain text in ~/.claude.json.
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="${1:-gmail-attachments-remote}"
URL="${2:-https://gmail-attachments-mcp.vercel.app}"

vercel whoami >/dev/null 2>&1 || { echo "Run 'vercel login' first."; exit 1; }
command -v claude >/dev/null || { echo "claude CLI not found on PATH."; exit 1; }

TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")')"

printf '%s' "$TOKEN" | vercel env add MCP_AUTH_TOKEN production --sensitive --force >/dev/null
vercel deploy --prod --yes >/dev/null
sleep 3

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL/mcp" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}')
[ "$code" = "200" ] || { echo "New token rejected by live server (HTTP $code). Not registering."; exit 1; }

claude mcp remove "$NAME" --scope user >/dev/null 2>&1 || true
claude mcp add --transport http "$NAME" --scope user "$URL/mcp" --header "Authorization: Bearer $TOKEN" >/dev/null

echo "Done. New token verified against $URL and registered as '$NAME'."
echo "Restart Claude Code to load it."
