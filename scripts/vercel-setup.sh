#!/usr/bin/env bash
# One-time: link the Vercel project and set the four env vars from your local
# Gmail OAuth files. Secrets are piped straight to Vercel, never printed.
# Prints the MCP_AUTH_TOKEN once at the end: save it, you need it as the bearer token.
set -euo pipefail
cd "$(dirname "$0")/.."

vercel whoami >/dev/null 2>&1 || { echo "Run 'vercel login' first."; exit 1; }
vercel link --yes

TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
eval "$(uv run gmail-attachments-export | sed 's/^/export /')"

for target in production preview; do
  printf '%s' "$GMAIL_ATT_CLIENT_ID"     | vercel env add GMAIL_ATT_CLIENT_ID     "$target" --sensitive --force >/dev/null
  printf '%s' "$GMAIL_ATT_CLIENT_SECRET" | vercel env add GMAIL_ATT_CLIENT_SECRET "$target" --sensitive --force >/dev/null
  printf '%s' "$GMAIL_ATT_REFRESH_TOKEN" | vercel env add GMAIL_ATT_REFRESH_TOKEN "$target" --sensitive --force >/dev/null
  printf '%s' "$TOKEN"                    | vercel env add MCP_AUTH_TOKEN           "$target" --sensitive --force >/dev/null
done

echo
echo "Env vars set. Your MCP bearer token (save it now, it is not shown again):"
echo "$TOKEN"
echo
echo "Next: vercel deploy --prod"
