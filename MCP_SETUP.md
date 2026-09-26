# Connect to the Lemnisca Media KB (MCP)

Server: `https://13-126-230-191.sslip.io/mcp`. It works from any network and needs a personal token.

## 1. Get your token
Ask the admin to create one. In the web app, that's **Access → type your name → Create token**. It looks like `lemkb_…`, is shown only once, and should be kept private. Tokens can be revoked at any time.

## 2a. Claude Code (Windows, Mac, Linux)
```bash
claude mcp add --scope user --transport http lemnisca-media-kb-remote https://13-126-230-191.sslip.io/mcp --header "Authorization: Bearer YOUR_TOKEN"
claude mcp get lemnisca-media-kb-remote      # should show: ✔ Connected
```

## 2b. Claude Desktop
You need **Node.js** (LTS, nodejs.org). In Claude Desktop, open **Settings → Developer → Edit Config**, add the block below to `claude_desktop_config.json`, and save.

- Mac: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

**Mac**
```json
{
  "mcpServers": {
    "lemnisca-media-kb-remote": {
      "command": "npx",
      "args": ["-y", "mcp-remote@latest", "https://13-126-230-191.sslip.io/mcp", "--header", "Authorization:${AUTH_HEADER}"],
      "env": { "AUTH_HEADER": "Bearer YOUR_TOKEN" }
    }
  }
}
```

**Windows:** the same block, but with `"command": "cmd"` and `"args": ["/c", "npx", "-y", "mcp-remote@latest", …the rest unchanged]`.

Notes:
- If the file already has other settings or an `"mcpServers"` section, add only the `"lemnisca-media-kb-remote": {…}` entry. Commas between entries matter.
- Keep `Authorization:${AUTH_HEADER}` exactly as written. Put your token only in the `AUTH_HEADER` line, after `Bearer ` (with the space).

Then **fully quit** Claude Desktop (Mac: Cmd+Q; Windows: right-click the tray icon → Quit) and reopen it. Under **Settings → Developer**, `lemnisca-media-kb-remote` should show as running.

## 3. Use it
Ask normally with the server switched on in the chat's tools menu, e.g. *"Plot stability against pH for arginine"* or *"What is known about glutamine stability? Cite papers."* Allow the tools when prompted. Answers cite DOIs, and charts appear in the chat.

To avoid naming the server in questions, add this instruction to a Claude Desktop Project, or to `~/.claude/CLAUDE.md` for Claude Code:
> For questions about cell-culture media components, solubility, stability, uptake, transporters or co-dependencies, use the lemnisca-media-kb-remote tools, cite DOIs and state the evidence level.

## Troubleshooting
| Problem | Fix |
|---|---|
| "failed" / `npx` not found (Mac) | Run `which npx` in Terminal and use that full path as `"command"`, e.g. `/opt/homebrew/bin/npx` |
| Server not listed | Invalid JSON, or the app wasn't fully quit. Fix the JSON, quit and reopen |
| 401 / unauthorized | Token wrong or revoked. Ask the admin for a new one |
| Claude answers without the tools | Turn the server on in the tools menu, or mention "lemnisca-media-kb-remote" |
