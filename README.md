# opencode-samcp

**S**ub-**A**gent **MCP** — an MCP server that lets an AI agent (Devin, Codex, Claude Code, Cursor, …) spawn [opencode](https://opencode.ai) sessions as sub-agents and hand them coding work.

The parent agent calls `opencode_run` with a prompt and a directory; opencode does the work headlessly (`opencode run --format json --auto`) and the result comes back as structured data: final message, tool-call summary, errors, token usage and cost. Sessions can be continued, forked, run in parallel in the background, inspected and deleted.

## Requirements

- [opencode](https://opencode.ai) installed and authenticated (`opencode` on `PATH`, or set `OPENCODE_BIN`)
- [uv](https://docs.astral.sh/uv/) (or `pip install`)

## Install / run

The server is Python; every option below needs [uv](https://docs.astral.sh/uv/) and opencode on the machine.

| Method | Command |
|---|---|
| **npx** (no install) | `npx -y opencode-samcp` |
| **npm** (global) | `npm i -g opencode-samcp` then `opencode-samcp` |
| **uvx** from PyPI | `uvx opencode-samcp` |
| **uvx** from GitHub | `uvx --from git+https://github.com/amirdaraby/opencode-samcp opencode-samcp` |
| **pip** | `pip install opencode-samcp` then `opencode-samcp` |

The npm package is a thin launcher: it bundles the Python server and runs it through `uvx`.
If `uv` or `opencode` is missing, it exits with a clear "not installed on machine" error.

## Configure your client

### Devin
Settings → MCP Marketplace → *Add Your Own* (STDIO):

| Field | Value |
|---|---|
| Command | `npx` |
| Args | `-y opencode-samcp` |

Devin runs in its own VM, so opencode must be installed and logged in **there** (add it to the machine setup / snapshot).

### Codex (`~/.codex/config.toml`)
```toml
[mcp_servers.opencode]
command = "npx"
args = ["-y", "opencode-samcp"]
```

### Claude Code
```bash
claude mcp add opencode -- npx -y opencode-samcp
```

### Generic JSON
```json
{ "mcpServers": { "opencode": { "command": "npx", "args": ["-y", "opencode-samcp"] } } }
```

### HTTP transport
For clients that only speak remote MCP:
```bash
opencode-samcp --transport streamable-http --host 127.0.0.1 --port 8765   # endpoint: /mcp
opencode-samcp --transport sse ...                                         # endpoint: /sse
```
There is **no authentication** and the sub-agent can edit files and run commands — keep it on localhost or behind an authenticated tunnel/proxy.

## Tools

| Tool | Purpose |
|---|---|
| `opencode_run` | Start a session (or continue `session_id`, optionally `fork`) in `directory`. Waits up to `wait_seconds` (default 600); `0` runs in the background. Supports `model`, `agent`, `title`, `files`, `auto_approve`. |
| `opencode_job` | Poll/wait on a background job by `job_id`. |
| `opencode_jobs` | List jobs started by this server. |
| `opencode_cancel` | Stop a running job (session is kept). |
| `opencode_sessions` | List opencode sessions for a project directory. |
| `opencode_transcript` | Condensed transcript of a session. |
| `opencode_delete_session` | Delete a session and its children. |

**Parallel work:** start several jobs with `wait_seconds=0`, each in its own git worktree so they don't clash, then poll with `opencode_job`.

## Environment variables

| Var | Meaning |
|---|---|
| `OPENCODE_BIN` | Path to the opencode binary (default `opencode`) |
| `OPENCODE_MODEL` | Default `provider/model` when the caller doesn't pass one |

## Security

`auto_approve` defaults to `true` (required for headless runs): the sub-agent can modify files and run shell commands in the given directory. Only expose this server to agents you trust, and point it at disposable checkouts/worktrees where possible.

## Notes

- Job state is in-memory; it is lost when the server process restarts (opencode sessions themselves persist and can be resumed by `session_id`).

## Publishing (maintainers)

```bash
npm publish            # npm / npx
uv build && uv publish # PyPI / uvx / pip
```

## Development

```bash
uv run --group dev pytest
```

MIT licensed.
