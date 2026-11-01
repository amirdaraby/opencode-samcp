import pytest

from opencode_samcp import server


def test_handle_event_collects_text_tools_and_usage():
    job = {"session_id": None, "texts": [], "tools": [], "errors": [],
           "tokens": {"input": 0, "output": 0}, "cost": 0.0}
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


def test_resolve_dir_rejects_missing(tmp_path):
    assert server._resolve_dir(str(tmp_path)) == str(tmp_path)
    with pytest.raises(ValueError):
        server._resolve_dir(str(tmp_path / "nope"))


async def test_tools_registered():
    names = {t.name for t in await server.mcp.list_tools()}
    assert names == {"opencode_run", "opencode_job", "opencode_jobs", "opencode_cancel",
                     "opencode_sessions", "opencode_transcript", "opencode_delete_session"}


async def test_unknown_job():
    with pytest.raises(ValueError):
        await server.opencode_job("missing")


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
        await server.opencode_run("x", str(tmp_path), fork=True)


async def test_run_job_failure_marks_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OPENCODE", "definitely-not-a-real-binary")
    r = await server.opencode_run("hi", str(tmp_path), wait_seconds=5)
    assert r["status"] == "failed" and r["errors"]


async def test_run_job_with_fake_opencode(tmp_path, monkeypatch):
    fake = tmp_path / "fake"
    fake.write_text('#!/bin/sh\necho \'{"type":"text","sessionID":"ses_x","part":{"text":"done"}}\'\n')
    fake.chmod(0o755)
    monkeypatch.setattr(server, "OPENCODE", str(fake))
    r = await server.opencode_run("hi", str(tmp_path), wait_seconds=10)
    assert r["status"] == "completed" and r["session_id"] == "ses_x" and r["final_text"] == "done"
