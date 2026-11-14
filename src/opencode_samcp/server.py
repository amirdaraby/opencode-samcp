"""MCP server that lets any MCP client (Devin, Codex, Claude Code, ...) delegate coding tasks to opencode and manage its sessions."""

import argparse
import asyncio
import json
import os
import shutil
import signal
import sys
import time
import uuid

from mcp.server.fastmcp import Context, FastMCP

OPENCODE = os.environ.get("OPENCODE_BIN", "opencode")
DEFAULT_MODEL = os.environ.get("OPENCODE_MODEL")  # unset -> opencode's configured default
STALL_SECONDS = int(os.environ.get("OPENCODE_STALL_SECONDS", "90"))  # no stream events for this long = stalled

mcp = FastMCP("opencode-samcp")
JOBS: dict[str, dict] = {}
MAX_JOBS = int(os.environ.get("OPENCODE_MAX_JOBS", "200"))  # finished jobs beyond this are evicted
_PHASES = {"step_start": "thinking", "text": "writing", "tool_use": "working"}


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
    job["last_event_at"] = time.time()
    job["last_event_type"] = kind
    if kind == "text" and part.get("text", "").strip():
        # streamed text parts are re-emitted with the same id: update in place
        tid = part.get("id")
        if tid and tid in job["text_index"]:
            job["texts"][job["text_index"][tid]] = part["text"].strip()
        else:
            if tid:
                job["text_index"][tid] = len(job["texts"])
            job["texts"].append(part["text"].strip())
    elif kind == "tool_use":
        st = part.get("state") or {}
        job["tools"].append({
            "tool": part.get("tool"),
            "status": st.get("status"),
            "input": _clip(st.get("input"), 300),
            "output": _clip(st.get("output") or "", 300),
            "ts": ev.get("timestamp"),
        })
    elif kind == "step_start":
        job["steps"] += 1
    elif kind == "step_finish":
        tok = part.get("tokens") or {}
        job["tokens"]["input"] += tok.get("input", 0)
        job["tokens"]["output"] += tok.get("output", 0)
        job["cost"] += part.get("cost") or 0
        job["step_reason"] = part.get("reason")
    elif kind == "error":
        err = ev.get("error") or {}
        job["errors"].append(err.get("message") or json.dumps(err))
    job["events"].append(_event_summary(kind, ev, part))
    del job["events"][: max(0, len(job["events"]) - 50)]  # ring buffer, newest last


def _event_summary(kind, ev, part):
    ts = ev.get("timestamp")
    entry = {"at": round(ts / 1000, 3) if ts else None, "type": kind}
    if kind == "tool_use":
        st = part.get("state") or {}
        entry["summary"] = f"{part.get('tool')}: {_clip(json.dumps(st.get('input')), 100)} [{st.get('status')}]"
    elif kind == "text":
        entry["summary"] = _clip(part.get("text", ""), 300)
    elif kind == "step_finish":
        entry["summary"] = f"reason={part.get('reason')} cost={part.get('cost')}"
    elif kind == "error":
        err = ev.get("error") or {}
        entry["summary"] = _clip(err.get("message") or json.dumps(err), 300)
    else:
        entry["summary"] = ""
    return entry


def _new_job(jid, directory, title, session_id):
    now = time.time()
    return {
        "id": jid, "status": "running", "directory": directory, "title": title,
        "session_id": session_id, "texts": [], "text_index": {}, "tools": [], "errors": [],
        "tokens": {"input": 0, "output": 0}, "cost": 0.0, "exit_code": None,
        "steps": 0, "step_reason": None, "events": [],
        "started": now, "finished": None, "last_event_at": now, "last_event_type": None,
        "proc": None,
    }


def _phase(job):
    if job["status"] != "running":
        return job["status"]
    if time.time() - job["last_event_at"] > STALL_SECONDS:
        return "stalled"
    return _PHASES.get(job["last_event_type"] or "", "working")


def _last_activity(job):
    if job["tools"]:
        t = job["tools"][-1]
        s = f"{t['tool']}: {_clip(t.get('input'), 100)} — {t['status']}"
        if t.get("ts"):
            s += f" {round(time.time() - t['ts'] / 1000, 1)}s ago"
        return s
    if job["texts"]:
        return f"assistant: {_clip(job['texts'][-1], 100)}"
    return "started"


def _progress(job):
    """Compact progress snapshot of a job: what's done, what's happening now, what the agent
    says is next, plus counts and usage. Kept small on purpose — it is polled by LLMs."""
    now = time.time()
    stalled = job["status"] == "running" and now - job["last_event_at"] > STALL_SECONDS
    done = []
    for t in job["tools"][-8:]:
        line = f"{t['tool']}: {_clip(t.get('input'), 80)}"
        if t["status"] != "completed":
            line += f" [{t['status']}]"
        done.append(line)
    return {
        "job_id": job["id"],
        "status": job["status"],
        "session_id": job["session_id"],
        "elapsed_s": round((job["finished"] or now) - job["started"], 1),
        "steps": job["steps"],
        "tool_calls": {
            "total": len(job["tools"]),
            "completed": sum(1 for t in job["tools"] if t["status"] == "completed"),
            "failed": sum(1 for t in job["tools"] if t["status"] == "error"),
        },
        "done": done,
        "doing": _last_activity(job),
        "next": _clip(job["texts"][-1], 300) if job["texts"] else "",
        "phase": _phase(job),
        "stalled": stalled,
        "last_event_ago_s": round(now - job["last_event_at"], 1),
        "tokens": job["tokens"],
        "cost": round(job["cost"], 4),
        "error": _clip(job["errors"][-1], 200) if job["errors"] else None,
    }


async def _run_job(job, args):
    try:
        proc = await asyncio.create_subprocess_exec(
            OPENCODE, *args, cwd=job["directory"],
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024, start_new_session=True,
        )
        job["proc"] = proc
        if job["status"] == "cancelled":  # cancel landed before the process was visible
            _kill_group(proc, signal.SIGKILL)
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
    except Exception as e:  # noqa: BLE001 - surface any failure on the job instead of hanging
        job["errors"].append(f"{type(e).__name__}: {e}")
        if job["status"] == "running":
            job["status"] = "failed"
        proc = job.get("proc")
        if proc and proc.returncode is None:
            _kill_group(proc, signal.SIGKILL)
    finally:
        job["finished"] = time.time()


def _kill_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _evict_old_jobs():
    done = [j for j in JOBS.values() if j["status"] != "running"]
    for j in sorted(done, key=lambda j: j["finished"] or 0)[: max(0, len(JOBS) - MAX_JOBS)]:
        JOBS.pop(j["id"], None)


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


async def _wait_with_progress(job, wait_seconds, report=None):
    """Race the job against the deadline, calling report(tick, phase) on each ~5s tick."""
    deadline = time.time() + max(wait_seconds, 0)
    tick = 0
    while job["status"] == "running":
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(asyncio.shield(job["task"]), timeout=min(5.0, remaining))
            break
        except asyncio.TimeoutError:
            pass
        tick += 1
        if report:
            try:
                await report(tick, _phase(job))
            except Exception:  # noqa: BLE001 - progress reporting must never kill the job
                pass


@mcp.tool()
async def opencode_run(
    ctx: Context,
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
    job_id — poll it with opencode_progress (compact snapshot: what's done, doing, next) or
    opencode_job (full report). While this call is waiting, clients that send an MCP progress
    token receive live updates (this also keeps very long waits from timing out). Use
    wait_seconds=0 to start in the background and run several sessions in parallel (use separate
    git worktrees so they don't clash).
    `auto_approve` lets opencode edit files/run commands without asking (required for headless use).
    `model` is "provider/model"; defaults to opencode's configured model.
    Returns the session_id, opencode's final message, a summary of tool calls, errors, and usage.
    """
    if fork and not session_id:
        raise ValueError("fork=True requires session_id")
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

    job = _new_job(uuid.uuid4().hex[:8], d, title, None if fork else session_id)
    job["task"] = asyncio.create_task(_run_job(job, args))
    JOBS[job["id"]] = job
    _evict_old_jobs()
    await _wait_with_progress(job, wait_seconds,
                              lambda t, p: ctx.report_progress(t, message=f"{job['steps']} steps, {len(job['tools'])} tool calls, {p}"))
    return _report(job)


@mcp.tool()
async def opencode_job(ctx: Context, job_id: str, wait_seconds: int = 0) -> dict:
    """Get the full status/result of an opencode_run job, optionally waiting up to `wait_seconds`
    for it to finish. For a compact mid-run snapshot (what's done/doing/next) use opencode_progress."""
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"unknown job_id {job_id}")
    if job["status"] == "running" and wait_seconds > 0:
        await _wait_with_progress(job, wait_seconds,
                                  lambda t, p: ctx.report_progress(t, message=f"{job['steps']} steps, {len(job['tools'])} tool calls, {p}"))
    return _report(job)


@mcp.tool()
async def opencode_progress(job_id: str, tail: int = 0) -> dict:
    """Compact progress snapshot of a job, cheap to poll while it runs.

    Reports what's done (recent completed tool calls), what's happening now (phase + last
    activity), and what the agent says is next (its latest message). Also: elapsed time,
    step/tool-call counts, tokens, cost, and a stall flag (no stream events for
    OPENCODE_STALL_SECONDS, default 90s). tail=N additionally returns the last N stream events.
    Use opencode_job for the full report with complete tool-call details."""
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"unknown job_id {job_id}")
    r = _progress(job)
    if tail > 0:
        r["events"] = job["events"][-tail:]
    return r


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
    if job["status"] == "running":
        job["status"] = "cancelled"
        # proc may not exist yet; _run_job kills it once it does
        proc = job.get("proc")
        if proc and proc.returncode is None:
            _kill_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(job["task"]), timeout=10)
        except asyncio.TimeoutError:
            proc = job.get("proc")
            if proc and proc.returncode is None:
                _kill_group(proc, signal.SIGKILL)
            await job["task"]
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


def check_opencode() -> str:
    """Return the resolved opencode binary path, or exit with a clear error if it is missing."""
    path = shutil.which(OPENCODE)
    if not path:
        sys.exit(
            f"opencode-samcp failed: opencode not installed on machine "
            f"('{OPENCODE}' not found on PATH). Install it from https://opencode.ai "
            f"or set OPENCODE_BIN to its path."
        )
    return path


def main() -> None:
    parser = argparse.ArgumentParser(prog="opencode-samcp", description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="bind address for HTTP transports")
    parser.add_argument("--port", type=int, default=8765, help="port for HTTP transports")
    args = parser.parse_args()
    check_opencode()
    if args.transport != "stdio" and args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: serving unauthenticated MCP on {args.host}:{args.port}; anyone who can reach it "
              "can run commands on this machine. Use localhost or an authenticated proxy.", file=sys.stderr)
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
