# MCP server

`xteink.mcp` lets an MCP client (Claude Code, an agent on the Culture mesh, any
MCP-capable tool) push files into the library and queue them for a reader. It is
a thin client of the [HTTP API](api.md): it holds no data of its own and calls
the main app with an API key. It is built on the official `mcp` Python SDK.

## Running it

The server needs the `server` extra (`pip install 'xteink[server]'`, or
`uv sync --extra server` in a checkout). The Docker image already has it.

### Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `XTEINK_URL` | `http://127.0.0.1:8780` | Main app URL |
| `XTEINK_API_KEY` | none | API key (`xtk_...`) for **stdio**. Required there; the server exits with code 2 and a message if it is missing. Not used by `--http`, where every caller sends its own key |
| `XTEINK_MCP_BIND` | `0.0.0.0` | Bind address for `--http` |
| `XTEINK_MCP_PORT` | `8782` | Port for `--http` |
| `XTEINK_MCP_TRANSPORT` | `stdio` | Set to `http` to act like `--http` |
| `XTEINK_MCP_ALLOWED_HOSTS` | none | Comma-separated `Host` values `--http` accepts (`host`, or `host:*` for any port). Set, it turns on the SDK's DNS-rebinding protection: another `Host` gets 421 |
| `XTEINK_MCP_ALLOWED_ORIGINS` | none | Comma-separated `Origin` values accepted with the above (a request without `Origin`, as from a server-side agent, passes); another gets 403 |

Mint a dedicated key for it so you can revoke it independently:

```bash
docker compose run --rm api python -m xteink.server create-key mcp
```

### stdio (local agents)

The default. The MCP client starts the process and talks over stdin/stdout:

```bash
XTEINK_URL=http://localhost:8780 XTEINK_API_KEY=<api-key> \
  uv run --extra server python -m xteink.mcp
```

`uv run xteink mcp serve` does the same through the CLI.

### streamable-http (LAN, tailnet, mesh)

```bash
python -m xteink.mcp --http --bind 0.0.0.0 --port 8782
```

In the compose stack this is the `mcp` service, published on host port 8782
(`XTEINK_PUBLISH_MCP_PORT`). The endpoint is `http://<host>:8782/mcp`; `/mcp/`
answers the same (no redirect: behind a TLS proxy one would point at `http://`).

Every request must carry `Authorization: Bearer xtk_...`, an xteink API key, and
the key is checked with the API (`GET /api/whoami`) before the request reaches an
MCP session:

| Request | Answer |
|---------|--------|
| No key, or not a `Bearer xtk_...` one | 401, `WWW-Authenticate: Bearer realm="xteink"` |
| A key the API refuses (unknown, revoked, a device key) | 401, `WWW-Authenticate: Bearer realm="xteink", error="invalid_token"` (RFC 6750) |
| The API cannot be reached | 503 |

A key the API accepted is trusted for 60 s (by its sha256) so a session's many
requests cost one check; a refused key is never remembered. Each tool call still
uses the caller's key against the API, which decides. The HTTP server holds no key
of its own. Over HTTP, `push_file` refuses `path` (a file on the MCP host); send
`content` or `content_base64` instead.

### Over the internet

The compose `remote` profile never tunnels the MCP port. A cloud agent (an agent
running on someone else's computer, such as Grok Bot) cannot reach a LAN, so on a
VPS the MCP server can sit behind the same reverse proxy as the API, at
`https://<host>/mcp` ([`vps-deploy.md`](vps-deploy.md#mcp-for-cloud-agents)). That
exposes nothing new: every tool is an API call made with the caller's key, the same
call `https://<host>/api/*` already accepts. Give each agent its own key so it can be
revoked alone, and set `XTEINK_MCP_ALLOWED_HOSTS` to the public host name.

## Client configuration

A `.mcp.json` for Claude Code, stdio, started from a checkout:

```json
{
  "mcpServers": {
    "xteink": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/xteink", "--extra", "server",
               "python", "-m", "xteink.mcp"],
      "env": {
        "XTEINK_URL": "http://localhost:8780",
        "XTEINK_API_KEY": "<api-key>"
      }
    }
  }
}
```

Or against the HTTP service on another machine:

```json
{
  "mcpServers": {
    "xteink": {
      "type": "http",
      "url": "http://<host-or-tailnet-name>:8782/mcp/",
      "headers": { "Authorization": "Bearer <api-key>" }
    }
  }
}
```

Keep real keys out of any file you commit.

## Tools

### push_file

Add a file to the library. EPUB, BMP and TXT are stored as-is; Markdown and HTML
are converted to EPUB. Give **exactly one** of `path`, `content` or
`content_base64`. Optionally queue it for a device in the same call.

| Argument | Type | Notes |
|----------|------|-------|
| `path` | string | File on the MCP host |
| `content` | string | File text, e.g. a Markdown article. Needs `filename` |
| `content_base64` | string | Binary file, base64. Needs `filename` |
| `filename` | string | e.g. `article.md`; the extension picks the format |
| `title`, `author` | string | Optional metadata |
| `kind` | `book`, `article`, `transcript` or `newspaper` | Optional |
| `device` | string or integer | Device id or name; queues the item for it |

Push a Markdown article and queue it for a reader called `my-reader`:

```json
{
  "name": "push_file",
  "arguments": {
    "filename": "why-local-first.md",
    "title": "Why local-first",
    "kind": "article",
    "content": "# Why local-first\n\nYour library stays on hardware you own.\n",
    "device": "my-reader"
  }
}
```

Result (JSON text): `{"item": {"id": 7, "title": "...", "kind": "article",
"format": "epub", "size": 18422}, "created": true, "queued": {...queue entry...}}`. `created`
is `false` when identical bytes were already in the library.

### save_url

Save a web page by its address. The API server fetches it, keeps only the
article and converts it to EPUB; a link to an EPUB, Markdown or text file is
saved as it is. Arguments: `url` (required), `title`, `author`, `kind`, and
`device` (an id or a unique name) to queue it for that reader.

```json
{"name": "save_url", "arguments": {"url": "https://example.org/essay", "device": "my-reader"}}
```

Result: `{"item": {...}, "created": true, "queued": {...queue entry...}}`, with
`queued` only when `device` was given. The server only fetches public
`http(s)` addresses. What it refuses, and the error codes, are in
[`api.md`](api.md#post-apilibraryurl). When a site refuses the fetch
(`fetch_failed (HTTP 502)`), fetch the page yourself and send its text with
`push_file`.

### list_library

List or search items. Arguments: `q` (searches title and author), `kind`
(`book`, `article`, `transcript` or `newspaper`), `limit` (1 to 1000, default 100).

```json
{"name": "list_library", "arguments": {"q": "local-first", "kind": "article"}}
```

Result: `{"items": [{"id": 7, "title": "...", ...}]}`.

### list_devices

The readers that can receive items, so an agent can find the name `device`
takes without being told. No arguments. Revoked readers are left out.

```json
{"name": "list_devices", "arguments": {}}
```

Result: `{"devices": [{"id": 1, "name": "x4", "last_seen": "...",
"last_sync_result": "ok", "firmware_version": "..."}]}`. Keys and run details
are not included.

### send_to_device

Queue an existing item so the device downloads it on its next sync. Both
arguments are required.

```json
{"name": "send_to_device", "arguments": {"device": "my-reader", "item_id": 7}}
```

Result: the queue entry. `device` may be an id or a unique name.

### Errors

A failed call returns an MCP tool error whose message starts with a short code,
for example `unknown_device`, `ambiguous_device`, `invalid_arguments`,
`unreadable_path`, `missing_key`, or an API error such as
`too_large (HTTP 413)`. The codes for rejected uploads are the ones in
[`api.md`](api.md#post-apilibrary), and those for `save_url` are in
[`api.md`](api.md#post-apilibraryurl).
