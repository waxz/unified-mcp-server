"""Interactive approval of commands that aren't in TRUSTED_COMMANDS."""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.transports import StreamableHttpTransport

from unified_mcp_server.config import load_settings
from unified_mcp_server.server import build_server
from unified_mcp_server.shell.approvals import APPROVALS

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell commands")


def _project(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir(exist_ok=True)
    return p


def _app(tmp_path: Path, *argv: str):
    return build_server(
        load_settings(["-c", str(tmp_path / "none.toml"), "-w", str(_project(tmp_path)), *argv])
    )


def _text(res) -> str:
    return "".join(getattr(c, "text", "") for c in res.content)


class Prompter:
    """Elicitation handler: records prompts and answers as told."""

    def __init__(self, answer: str = "Allow until restart", delay: float = 0.0):
        self.answer = answer
        self.delay = delay
        self.messages: list[str] = []

    async def __call__(self, message, response_type, params, context):
        self.messages.append(message)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.answer in ("decline", "cancel"):
            return ElicitResult(action=self.answer)
        return self.answer


async def _run(client, command, cwd=".", **kw):
    return await client.call_tool("execute_command", {"command": command, "cwd": cwd, **kw}, raise_on_error=False)


# ----------------------------------------------------------- client prompt
async def test_allow_until_restart_runs_and_is_remembered(tmp_path):
    prompter = Prompter("Allow until restart")
    async with Client(_app(tmp_path), elicitation_handler=prompter) as client:
        res = await _run(client, "echo remembered")
        assert "remembered" in _text(res) and len(prompter.messages) == 1
        await _run(client, "echo remembered")
        assert len(prompter.messages) == 1  # no second prompt
        await _run(client, "echo different")
        assert len(prompter.messages) == 2


async def test_allow_once_asks_again_next_time(tmp_path):
    prompter = Prompter("Allow once")
    async with Client(_app(tmp_path), elicitation_handler=prompter) as client:
        assert "once-1" in _text(await _run(client, "echo once-1"))
        assert "once-1" in _text(await _run(client, "echo once-1"))
    assert len(prompter.messages) == 2


async def test_deny_does_not_run(tmp_path):
    marker = _project(tmp_path) / "pwned"
    async with Client(_app(tmp_path), elicitation_handler=Prompter("decline")) as client:
        res = await _run(client, f"touch {marker}")
    assert res.is_error and "denied" in _text(res)
    assert not marker.exists()


async def test_prompt_waits_longer_than_page_wait(tmp_path):
    """While the client shows its prompt, the call waits for the answer, not APPROVAL_WAIT."""
    prompter = Prompter("Allow once", delay=2.5)
    async with Client(_app(tmp_path, "--approval-wait", "1"), elicitation_handler=prompter) as client:
        res = await _run(client, "echo slow-answer")
    assert not res.is_error and "slow-answer" in _text(res)


async def test_dismissed_prompt_leaves_request_open(tmp_path):
    async with Client(
        _app(tmp_path, "--approval-wait", "1"), elicitation_handler=Prompter("cancel")
    ) as client:
        res = await _run(client, "echo later")
    assert res.is_error and "Waiting for the user to approve" in _text(res)
    assert [r.key[0] for r in APPROVALS.pending()] == ["echo later"]


async def test_stdio_without_prompts_refuses_with_guidance(tmp_path):
    async with Client(_app(tmp_path)) as client:
        res = await _run(client, "echo hi")
    assert res.is_error and "TRUSTED_COMMANDS" in _text(res)
    assert APPROVALS.pending() == []


async def test_cwd_outside_allowed_roots_refused_before_asking(tmp_path):
    prompter = Prompter()
    async with Client(_app(tmp_path), elicitation_handler=prompter) as client:
        res = await _run(client, "echo hi", cwd="/")
    assert res.is_error and "Directory not allowed" in _text(res) and prompter.messages == []


async def test_no_approval_flag_restores_refusal(tmp_path):
    prompter = Prompter()
    async with Client(_app(tmp_path, "--no-approval"), elicitation_handler=prompter) as client:
        res = await _run(client, "echo hi")
    assert res.is_error and prompter.messages == []


async def test_trusted_commands_need_no_approval(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[TRUSTED_COMMANDS.t_hi]\ncommand = "echo trusted-hi"\nshell = "bash"\ncwd = "."\n')
    prompter = Prompter("decline")
    app = build_server(load_settings(["-c", str(cfg), "-w", str(_project(tmp_path))]))
    async with Client(app, elicitation_handler=prompter) as client:
        res = await _run(client, "t_hi")
    assert "trusted-hi" in _text(res) and prompter.messages == []


async def test_sandbox_configured_skips_approval(tmp_path):
    from unified_mcp_server import config
    from unified_mcp_server.shell.approvals import authorize

    _app(tmp_path)
    config.SETTINGS.DOCKER_SANDBOX_WORKDIR = "/app"
    assert await authorize("echo hi", ".", "bash", ctx=None) is None


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
async def test_tmux_execute_with_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    prompter = Prompter()
    try:
        async with Client(_app(tmp_path), elicitation_handler=prompter) as client:
            res = await client.call_tool(
                "tmux_execute", {"command": "echo from-tmux-$((20+1))", "session_name": "appr"}
            )
            assert "native_mcp_appr" in _text(res) and len(prompter.messages) == 1
            out = ""
            for _ in range(50):
                out = _text(await client.call_tool("tmux_get_output", {"session_name": "native_mcp_appr"}))
                if "from-tmux-21" in out:
                    break
                await anyio.sleep(0.1)
            assert "from-tmux-21" in out
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


# ------------------------------------------------- HTTP: dashboard and pages
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _http_server(tmp_path, *argv):
    port = _free_port()
    app = _app(tmp_path, "-t", "http", "-H", "127.0.0.1", "-P", str(port), *argv)
    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: app.run_http_async(host="127.0.0.1", port=port, path="/mcp", show_banner=False))
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                await anyio.sleep(0.05)
        base = f"http://127.0.0.1:{port}"
        async with Client(StreamableHttpTransport(f"{base}/mcp")) as mcp, httpx.AsyncClient(base_url=base) as web:
            yield mcp, web
        tg.cancel_scope.cancel()


async def test_pending_then_dashboard_approval_then_retry(tmp_path):
    marker = _project(tmp_path) / "ran.txt"
    cmd = f"echo dash-ok > {marker} && cat {marker}"
    async with _http_server(tmp_path, "--approval-wait", "1") as (mcp, web):
        key = APPROVALS.dashboard_token
        res = await _run(mcp, cmd)
        assert res.is_error and "Waiting for the user to approve" in _text(res) and not marker.exists()
        # Nothing secret reaches the model.
        assert key not in _text(res) and "token=" not in _text(res)

        assert (await web.get("/approvals?key=wrong")).status_code == 404
        assert (await web.get("/approvals/state?key=wrong")).status_code == 404
        page = await web.get(f"/approvals?key={key}")
        assert page.status_code == 200 and "Command approvals" in page.text
        state = (await web.get(f"/approvals/state?key={key}")).json()
        assert [p["command"] for p in state["pending"]] == [cmd]

        # A retry before anyone answers reuses the same request.
        await _run(mcp, cmd)
        assert len((await web.get(f"/approvals/state?key={key}")).json()["pending"]) == 1

        bad = await web.post("/approvals/decide", json={"key": "wrong", "id": state["pending"][0]["id"], "decision": "session"})
        assert bad.status_code == 404
        ok = await web.post("/approvals/decide", json={"key": key, "id": state["pending"][0]["id"], "decision": "session"})
        assert ok.json() == {"ok": True}

        res = await _run(mcp, cmd)
        assert not res.is_error and "dash-ok" in _text(res)
        state = (await web.get(f"/approvals/state?key={key}")).json()
        assert state["pending"] == [] and [a["command"] for a in state["approved"]] == [cmd]

        # Revoke: the next run needs approval again.
        a = state["approved"][0]
        rv = await web.post("/approvals/revoke", json={"key": key, **a})
        assert rv.json() == {"ok": True}, (rv.status_code, rv.text, a, list(APPROVALS.approved))
        res = await _run(mcp, cmd)
        assert res.is_error and "Waiting for the user to approve" in _text(res), (res.is_error, _text(res))


async def test_deny_from_dashboard_reaches_retry(tmp_path):
    async with _http_server(tmp_path, "--approval-wait", "1") as (mcp, web):
        key = APPROVALS.dashboard_token
        await _run(mcp, "echo nope")
        rid = (await web.get(f"/approvals/state?key={key}")).json()["pending"][0]["id"]
        await web.post("/approvals/decide", json={"key": key, "id": rid, "decision": "deny"})
        res = await _run(mcp, "echo nope")
        assert res.is_error and "denied" in _text(res)


async def test_per_request_page_allow_once(tmp_path):
    async with _http_server(tmp_path, "--approval-wait", "1") as (mcp, web):
        await _run(mcp, "echo via-page")
        req = APPROVALS.pending()[0]
        assert (await web.get(f"/approvals/{req.id}?token=wrong")).status_code == 404
        page = await web.get(f"/approvals/{req.id}?token={req.token}")
        assert "echo via-page" in page.text and "Allow until restart" in page.text
        r = await web.post(f"/approvals/{req.id}", data={"token": req.token, "decision": "once"})
        assert "Allow once" in r.text
        assert "via-page" in _text(await _run(mcp, "echo via-page"))
        # Once means once.
        res = await _run(mcp, "echo via-page")
        assert res.is_error and "Waiting for the user to approve" in _text(res)


async def test_request_expires(tmp_path):
    async with _http_server(tmp_path, "--approval-wait", "5", "--approval-timeout", "1") as (mcp, _web):
        res = await _run(mcp, "echo too-late")
    assert res.is_error and "expired" in _text(res)
    assert APPROVALS.pending() == []


def test_dashboard_url_only_over_http(tmp_path):
    from unified_mcp_server.shell.approvals import dashboard_url

    _app(tmp_path)
    assert dashboard_url() is None
    _app(tmp_path, "-t", "http", "-H", "127.0.0.1", "-P", "8123")
    url = dashboard_url()
    assert url.startswith("http://127.0.0.1:8123/approvals?key=") and APPROVALS.dashboard_token in url


def test_new_server_gets_new_dashboard_token(tmp_path):
    _app(tmp_path)
    first = APPROVALS.dashboard_token
    _app(tmp_path)
    assert APPROVALS.dashboard_token != first
