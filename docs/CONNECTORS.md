# Connectors: choice of Gmail/Drive MCP server

Decision, 2026-10-19: use **[taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp)**. It runs as one internal container in its *external OAuth provider, stateless* mode.

It is pinned to commit `301151aec23b672f6ca2067451cd4f9f6e34cc41` (v2.1.0, 2026-10-18), and the image is built from that commit (see `docker-compose.yml`). Review the diff before moving the pin.

## How it fits Relay

- **Relay holds the credentials.**
  - Relay does the per-user Google OAuth (`/api/connectors/google/*`).
  - It stores each refresh token Fernet-encrypted in `oauth_tokens`.
  - It mints short-lived access tokens, kept in memory only.
- **One request, one token.** Each tool listing and each tool call opens an MCP session with `Authorization: Bearer <that user's access token>`.
  - The server validates the token with Google and acts as that user.
  - Relay drops any `user_google_email` argument the model sends.
  - The server writes nothing to disk (`WORKSPACE_MCP_STATELESS_MODE`).
- **Relay keeps its own allowlist.** `config/connectors.yaml` lists every tool Relay will offer and marks each as read or write.
  - Tools the server exposes but the file does not list are never offered. A server upgrade therefore cannot add capabilities silently.
  - Write tools pause the agent loop for Approve/Reject in the UI (`approval_required`); rejecting sends nothing.
  - The allowlist does not rely on the server's `readOnlyHint`.
- **Scopes are the real boundary.** Relay asks Google only for the scopes listed in `connectors.yaml`. Even a tool listed by mistake fails without its scope.
- **Roll out in stages**, by editing `scopes`. Users reconnect after each change.
  1. `drive.readonly`
  2. add `gmail.readonly`
  3. add `gmail.compose` (drafts)
  4. add `gmail.send`

  `openid` and `email` are always needed: the server identifies the user from the token.
- **Network.** The container sits on the compose network only, with no published port.
- **Disabled server tools.** Some tools stage files behind a server URL, so they are switched off on the server as well: `get_drive_file_download_url`, `get_gmail_attachment_content`, and `start_google_auth`.

## Options considered

| Option | Verdict | Why |
|---|---|---|
| **A. taylorwilsdon/google_workspace_mcp** (MIT, Python, ~3.4k stars, active) | **Chosen** | It was the only maintained Gmail + Drive server found that accepts a per-request bearer token from an outside OAuth system. It stores no tokens, can filter tools, and needs no per-user processes. |
| **B. Google's official remote MCP servers** (`gmailmcp.googleapis.com`, `drivemcp.googleapis.com`) | Revisit at GA | No third-party code, but still Developer Preview as of 2026-10. Tools cannot be filtered on the server, and eligibility rules are unclear. |
| **C. piotr-agier/google-drive-mcp** (MIT, Node) | Rejected | Drive only, no Gmail. A per-user stdio process with the token in an env var. No tool filtering. |
| **D. In-house MCP server or direct REST calls** | Fallback | Exact scopes and no third-party code over mailboxes. In exchange, Relay would own MIME parsing, threading, exports and pagination: roughly 300 to 600 lines. |

Also rejected:

- `googleworkspace/cli`: its MCP mode was removed.
- `gemini-cli-extensions/workspace` and `aaronsb/google-workspace-mcp`: single user, tokens stored locally.
- `GongRzhe/Gmail-MCP-Server` and `shinzo-labs/gmail-mcp`: no longer maintained.

## Setup checklist (admin)

1. Create an OAuth client in Google Cloud Console:
   - Type: Web application.
   - Redirect URI: `https://<RELAY_DOMAIN>/api/connectors/google/callback`.
   - **Tick "Use this client for an AI-powered agent".** Search results report that Google rejects OAuth clients without this flag from 2026-11-11 for agentic use. I could not verify this against Google's own docs from the build environment, so check it.
2. If PaiKane's Google Workspace restricts third-party apps, have the Workspace admin trust the app (SPEC open question 4).
3. Enable the Gmail API and the Drive API in the project.
4. Put `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `FERNET_KEY` in SSM under `/relay/prod/`, then redeploy.
5. Each user connects in **Settings → Connectors**. Disconnecting revokes the token at Google and deletes Relay's copy.

## Not yet verified

This was checked by reading the server's code; none of it has been run:

- the exact format of `WORKSPACE_MCP_DISABLED_TOOLS`;
- whether external mode needs `GOOGLE_OAUTH_CLIENT_ID` (it is set anyway).

Relay's allowlist does not depend on either. On first deploy, check these by connecting a test account and asking "list my Drive files".

The CRM connector waits on the choice of CRM (SPEC open question 2).
