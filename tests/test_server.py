import asyncio
import contextlib
import time

import pytest

from opencode_samcp import server


class _FakeCtx:
    def __init__(self):
        self.reports = []

    async def report_progress(self, progress, total=None, message=None):
        self.reports.append((progress, message))


def _new_job(jid="j1", **overrides):
    job = server._new_job(jid, "/tmp", None, None)
    job.update(overrides)
    return job


def test_handle_event_collects_text_tools_and_usage():
    job = _new_job()
    server._handle_event(job, {"type": "text", "sessionID": "ses_1", "part": {"text": " hi "}})
    server._handle_event(job, {"type": "tool_use", "part": {"tool": "bash", "state": {"status": "completed", "input": {"c": 1}, "output": "ok"}}})
    server._handle_event(job, {"type": "step_finish", "part": {"tokens": {"input": 3, "output": 4}, "cost": 0.5}})
    server._handle_event(job, {"type": "error", "error": {"message": "boom"}})
    assert job["session_id"] == "ses_1"
    assert job["texts"] == ["hi"]
    assert job["tools"][0]["tool"] == "bash"
    assert job["tokens"] == {"input": 3, "output": 4}
    assert job["cost"] == 0.5
    assert job["errors"] == ["boom"]


def test_handle_event_tracks_progress_state():
    job = _new_job()
    server._handle_event(job, {"type": "step_start", "timestamp": 1000, "part": {"type": "step-start"}})
    server._handle_event(job, {"type": "step_finish", "timestamp": 2000, "part": {"reason": "tool-calls", "tokens": {}, "cost": 0.1}})
    assert job["steps"] == 1
    assert job["step_reason"] == "tool-calls"
    assert job["last_event_type"] == "step_finish"
    assert abs(job["last_event_at"] - time.time()) < 5
    assert job["events"][0]["type"] == "step_start" and job["events"][0]["at"] == 1.0
    assert job["events"][-1]["summary"].startswith("reason=tool-calls")


def test_handle_event_text_upserts_by_part_id():
    job = _new_job()
    server._handle_event(job, {"type": "text", "part": {"id": "p1", "text": "hel"}})
    server._handle_event(job, {"type": "text", "part": {"id": "p1", "text": "hello"}})
    server._handle_event(job, {"type": "text", "part": {"id": "p2", "text": "world"}})
    assert job["texts"] == ["hello", "world"]
    # text parts without ids keep appending
    server._handle_event(job, {"type": "text", "part": {"text": "a"}})
    server._handle_event(job, {"type": "text", "part": {"text": "b"}})
    assert job["texts"] == ["hello", "world", "a", "b"]


def test_handle_event_tool_entry_carries_timestamp():
    job = _new_job()
    server._handle_event(job, {"type": "tool_use", "timestamp": 12345,
                               "part": {"tool": "bash", "state": {"status": "completed", "input": {"c": 1}, "output": "ok"}}})
    assert job["tools"][0]["ts"] == 12345


def test_events_ring_buffer_capped_at_50():
    job = _new_job()
    for i in range(60):
        server._handle_event(job, {"type": "step_start", "timestamp": 1000 + i * 1000, "part": {"type": "step-start"}})
    assert job["steps"] == 60
    assert len(job["events"]) == 50
    assert job["events"][0]["at"] == 11.0
    assert job["events"][-1]["at"] == 60.0


def test_phase_mapping_and_stall():
    job = _new_job()
    assert server._phase(job) == "working"  # no events yet
    job["last_event_type"] = "step_start"
    assert server._phase(job) == "thinking"
    job["last_event_type"] = "text"
    assert server._phase(job) == "writing"
    job["status"] = "completed"
    assert server._phase(job) == "completed"
    job2 = _new_job("j2")
    job2["last_event_at"] = time.time() - (server.STALL_SECONDS + 10)
    assert server._phase(job2) == "stalled"


async def test_progress_report_shape():
    job = _new_job("j1")
    job["steps"] = 3
    job["last_event_type"] = "step_start"
    job["tools"] = [{"tool": "bash", "status": "completed", "input": {"command": "ls"}, "output": "",
                     "ts": int(time.time() * 1000)}]
    job["texts"] = ["Now writing tests"]
    job["events"] = [{"at": 1.0, "type": "text", "summary": "Now writing tests"}]
    server.JOBS["j1"] = job
    try:
        r = await server.opencode_progress("j1")
        assert r["job_id"] == "j1" and r["status"] == "running"
        assert r["steps"] == 3
        assert r["tool_calls"] == {"total": 1, "completed": 1, "failed": 0}
        assert r["done"] == ['bash: {"command": "ls"}']
        assert r["doing"].startswith("bash:")
        assert r["next"] == "Now writing tests"
        assert r["phase"] == "thinking"
        assert r["stalled"] is False
        assert r["error"] is None
        assert "events" not in r
        assert r["elapsed_s"] >= 0
        r2 = await server.opencode_progress("j1", tail=5)
        assert r2["events"] == job["events"]
    finally:
        server.JOBS.pop("j1", None)


async def test_progress_stalled():
    job = _new_job("j2")
    job["last_event_at"] = time.time() - (server.STALL_SECONDS + 10)
    server.JOBS["j2"] = job
    try:
        r = await server.opencode_progress("j2")
        assert r["stalled"] is True and r["phase"] == "stalled"
    finally:
        server.JOBS.pop("j2", None)


async def test_unknown_progress_job():
    with pytest.raises(ValueError):
        await server.opencode_progress("missing")


async def test_jobs_list_stays_lightweight():
    running = _new_job("jr")
    running["steps"] = 3
    server.JOBS.update({"jr": running})
    try:
        out = {r["job_id"]: r for r in await server.opencode_jobs()}
        assert "progress" not in out["jr"]  # progress data only via opencode_progress
        assert out["jr"]["status"] == "running"
    finally:
        server.JOBS.pop("jr", None)


async def test_wait_with_progress_ticks_until_deadline():
    job = _new_job("j3")
    async def _slow():
        await asyncio.sleep(3)
    job["task"] = asyncio.create_task(_slow())
    reports = []

    async def report(tick, phase):
        reports.append((tick, phase))

    try:
        await server._wait_with_progress(job, 0.4, report)
        assert [t for t, _ in reports] == [1]
        assert reports[0][1] == "working"
        assert job["status"] == "running"
    finally:
        job["task"].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await job["task"]


async def test_wait_with_progress_returns_when_done():
    job = _new_job("j4")
    job["status"] = "completed"
    job["finished"] = time.time()
    async def _done():
        return
    job["task"] = asyncio.create_task(_done())
    reports = []
    await server._wait_with_progress(job, 10, lambda t, p: reports.append(t))
    assert reports == []


def test_resolve_dir_rejects_missing(tmp_path):
    assert server._resolve_dir(str(tmp_path)) == str(tmp_path)
    with pytest.raises(ValueError):
        server._resolve_dir(str(tmp_path / "nope"))


async def test_tools_registered():
    names = {t.name for t in await server.mcp.list_tools()}
    assert names == {"opencode_run", "opencode_job", "opencode_progress", "opencode_jobs", "opencode_cancel",
                     "opencode_sessions", "opencode_transcript", "opencode_delete_session"}


async def test_unknown_job():
    with pytest.raises(ValueError):
        await server.opencode_job(ctx=_FakeCtx(), job_id="missing")


def test_check_opencode_missing(monkeypatch):
    monkeypatch.setattr(server, "OPENCODE", "definitely-not-a-real-binary")
    with pytest.raises(SystemExit) as e:
        server.check_opencode()
    assert "opencode not installed on machine" in str(e.value)


def test_check_opencode_found(monkeypatch):
    monkeypatch.setattr(server, "OPENCODE", "sh")
    assert server.check_opencode().endswith("sh")


async def test_fork_requires_session(tmp_path):
    with pytest.raises(ValueError):
        await server.opencode_run(prompt="x", directory=str(tmp_path), fork=True, ctx=_FakeCtx())


async def test_run_job_failure_marks_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OPENCODE", "definitely-not-a-real-binary")
    r = await server.opencode_run(prompt="hi", directory=str(tmp_path), wait_seconds=5, ctx=_FakeCtx())
    assert r["status"] == "failed" and r["errors"]


async def test_run_job_with_fake_opencode(tmp_path, monkeypatch):
    fake = tmp_path / "fake"
    fake.write_text('#!/bin/sh\necho \'{"type":"text","sessionID":"ses_x","part":{"text":"done"}}\'\n')
    fake.chmod(0o755)
    monkeypatch.setattr(server, "OPENCODE", str(fake))
    ctx = _FakeCtx()
    r = await server.opencode_run(prompt="hi", directory=str(tmp_path), wait_seconds=10, ctx=ctx)
    assert r["status"] == "completed" and r["session_id"] == "ses_x" and r["final_text"] == "done"
    assert "progress" not in r
