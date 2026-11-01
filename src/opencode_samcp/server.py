"""MCP server that lets any MCP client (Devin, Codex, Claude Code, ...) delegate coding tasks to opencode and manage its sessions."""

import argparse
import asyncio
import json
import os
import time
import uuid

from mcp.server.fastmcp import FastMCP

OPENCODE = os.environ.get("OPENCODE_BIN", "opencode")
DEFAULT_MODEL = os.environ.get("OPENCODE_MODEL")  # unset -> opencode's configured default

mcp = FastMCP("opencode-samcp")
JOBS: dict[str, dict] = {}


def _clip(s, n):
    s = s if isinstance(s, str) else json.dumps(s)
    return s if len(s) <= n else s[:n] + f"... [+{len(s) - n} chars]"


def _resolve_dir(directory):
    d = os.path.abspath(os.path.expanduser(directory))
    if not os.path.isdir(d):
        raise ValueError(f"directory does not exist: {d}")
    return d


async def _cli(args, cwd):
    proc = await asyncio.create_subprocess_exec(
        OPENCODE, *args, cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"opencode {' '.join(args)} failed ({proc.returncode}): {err.decode()[-2000:]}")
    return out.decode()


def _handle_event(job, ev):
    job["session_id"] = job["session_id"] or ev.get("sessionID")
    part = ev.get("part") or {}
    kind = ev.get("type")
    if kind == "text" and part.get("text", "").strip():
        job["texts"].append(part["text"].strip())
    elif kind == "tool_use":
        st = part.get("state") or {}
        job["tools"].append({
            "tool": part.get("tool"),
            "status": st.get("status"),
            "input": _clip(st.get("input"), 300),
            "output": _clip(st.get("output") or "", 300),
        })
    elif kind == "step_finish":
        tok = part.get("tokens") or {}
        job["tokens"]["input"] += tok.get("input", 0)
        job["tokens"]["output"] += tok.get("output", 0)
        job["cost"] += part.get("cost") or 0
    elif kind == "error":
        err = ev.get("error") or {}
        job["errors"].append(err.get("message") or json.dumps(err))


async def _run_job(job, args):
    proc = await asyncio.create_subprocess_exec(
        OPENCODE, *args, cwd=job["directory"],
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
    )
    job["proc"] = proc
    stderr_task = asyncio.create_task(proc.stderr.read())
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            _handle_event(job, json.loads(line))
        except json.JSONDecodeError:
            job["errors"].append(_clip(line, 500))
    job["exit_code"] = await proc.wait()
    stderr = (await stderr_task).decode(errors="replace").strip()
    if job["exit_code"] != 0 and stderr:
        job["errors"].append(_clip(stderr, 2000))
    if job["status"] == "running":
        job["status"] = "failed" if job["exit_code"] != 0 or (job["errors"] and not job["texts"]) else "completed"
    job["finished"] = time.time()


def _report(job, full=True):
    r = {
        "job_id": job["id"],
        "status": job["status"],
        "session_id": job["session_id"],
        "directory": job["directory"],
        "title": job["title"],
        "elapsed_s": round((job["finished"] or time.time()) - job["started"], 1),
    }
    if full:
        r.update({
            "final_text": job["texts"][-1] if job["texts"] else "",
            "tool_calls": job["tools"][-40:],
            "tool_call_count": len(job["tools"]),
            "errors": job["errors"],
            "tokens": job["tokens"],
            "cost": round(job["cost"], 4),
        })
        if len(job["texts"]) > 1:
            r["earlier_text"] = [_clip(t, 600) for t in job["texts"][-6:-1]]
    return r


async def _wait(job, wait_seconds):
    try:
        await asyncio.wait_for(asyncio.shield(job["task"]), timeout=max(wait_seconds, 0))
    except asyncio.TimeoutError:
        pass


@mcp.tool()
async def opencode_run(
    prompt: str,
    directory: str,
    session_id: str | None = None,
    fork: bool = False,
    model: str | None = None,
    agent: str | None = None,
    title: str | None = None,
    files: list[str] | None = None,
    auto_approve: bool = True,
    wait_seconds: int = 600,
) -> dict:
    """Send a coding task to opencode in `directory`.

    Starts a new opencode session, or continues `session_id` (with `fork=True`, branches it first).
    Waits up to `wait_seconds` for completion; if still running, returns status "running" with a
    job_id — poll it with opencode_job. Use wait_seconds=0 to start in the background and run
    several sessions in parallel (use separate git worktrees so they don't clash).
    `auto_approve` lets opencode edit files/run commands without asking (required for headless use).
    `model` is "provider/model"; defaults to opencode's configured model.
    Returns the session_id, opencode's final message, a summary of tool calls, errors, and usage.
    """
    d = _resolve_dir(directory)
    args = ["run", "--format", "json"]
    if auto_approve:
        args.append("--auto")
    if session_id:
        args += ["--session", session_id]
        if fork:
            args.append("--fork")
    model = model or DEFAULT_MODEL
    if model:
        args += ["--model", model]
    if agent:
        args += ["--agent", agent]
    if title:
        args += ["--title", title]
    for f in files or []:
        args += ["--file", f]
    # opencode duplicates args after "--", so pass the prompt positionally; a leading space
    # stops a prompt starting with "-" from being parsed as a flag.
    args.append(" " + prompt if prompt.startswith("-") else prompt)

    job = {
        "id": uuid.uuid4().hex[:8], "status": "running", "directory": d, "title": title,
        "session_id": None if fork else session_id, "texts": [], "tools": [], "errors": [],
        "tokens": {"input": 0, "output": 0}, "cost": 0.0, "exit_code": None,
        "started": time.time(), "finished": None, "proc": None,
    }
    job["task"] = asyncio.create_task(_run_job(job, args))
    JOBS[job["id"]] = job
    await _wait(job, wait_seconds)
    return _report(job)


@mcp.tool()
async def opencode_job(job_id: str, wait_seconds: int = 0) -> dict:
    """Get the status/result of an opencode_run job, optionally waiting up to `wait_seconds` for it to finish."""
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"unknown job_id {job_id}")
    if job["status"] == "running" and wait_seconds > 0:
        await _wait(job, wait_seconds)
    return _report(job)


@mcp.tool()
async def opencode_jobs() -> list[dict]:
    """List opencode_run jobs started by this server (running and finished)."""
    return [_report(j, full=False) for j in JOBS.values()]


@mcp.tool()
async def opencode_cancel(job_id: str) -> dict:
    """Stop a running opencode job. The session is kept and can be continued later."""
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"unknown job_id {job_id}")
    if job["status"] == "running" and job["proc"] and job["proc"].returncode is None:
        job["status"] = "cancelled"
        job["proc"].terminate()
        try:
            await asyncio.wait_for(asyncio.shield(job["task"]), timeout=10)
        except asyncio.TimeoutError:
            job["proc"].kill()
    return _report(job, full=False)


@mcp.tool()
async def opencode_sessions(directory: str, limit: int = 20) -> list[dict]:
    """List opencode sessions for the project containing `directory`, newest first."""
    d = _resolve_dir(directory)
    out = await _cli(["session", "list", "--format", "json", "-n", str(limit)], d)
    return json.loads(out or "[]")


@mcp.tool()
async def opencode_transcript(session_id: str, directory: str, max_chars: int = 30000) -> dict:
    """Get a condensed transcript of an opencode session: user prompts, assistant text, and tool calls with results."""
    d = _resolve_dir(directory)
    data = json.loads(await _cli(["session", "export", session_id], d))
    info = data.get("info", {})
    lines = []
    for msg in data.get("messages", []):
        if msg.get("type") == "user":
            lines.append(f"USER: {msg.get('text', '')}")
            continue
        for c in msg.get("content", []):
            if c.get("type") == "text" and c.get("text", "").strip():
                lines.append(f"ASSISTANT: {c['text'].strip()}")
            elif c.get("type") == "tool":
                st = c.get("state") or {}
                result = " ".join(x.get("text", "") for x in st.get("content") or [] if isinstance(x, dict))
                lines.append(f"TOOL {c.get('name')} [{st.get('status')}] {_clip(st.get('input'), 400)}"
                             f"\n  -> {_clip(result, 400)}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = f"[... {len(text) - max_chars} earlier chars omitted ...]\n" + text[-max_chars:]
    return {
        "session_id": info.get("id", session_id),
        "outcome": info.get("outcome"),
        "model": info.get("model"),
        "tokens": info.get("tokens"),
        "cost": info.get("cost"),
        "transcript": text,
    }


@mcp.tool()
async def opencode_delete_session(session_id: str, directory: str) -> str:
    """Delete an opencode session and its child sessions."""
    d = _resolve_dir(directory)
    await _cli(["session", "delete", session_id], d)
    return f"deleted {session_id}"


def main() -> None:
    parser = argparse.ArgumentParser(prog="opencode-samcp", description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="bind address for HTTP transports")
    parser.add_argument("--port", type=int, default=8765, help="port for HTTP transports")
    args = parser.parse_args()
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
