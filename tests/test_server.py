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
