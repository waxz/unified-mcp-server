"""Interactive approval of commands that aren't in TRUSTED_COMMANDS."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import socket
import sys
from pathlib import Path

import anyio
import httpx
import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.transports import StreamableHttpTransport

from unified_mcp_server.config import load_settings
from unified_mcp_server.server import build_server

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
    """Elicitation handler that records prompts and answers as told."""

    def __init__(self, answer: str = "accept", delay: float = 0.0):
        self.answer = answer
        self.delay = delay
        self.messages: list[str] = []

    async def __call__(self, message, response_type, params, context):
        self.messages.append(message)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.answer == "accept":
            return {}
        return ElicitResult(action=self.answer)


# ------------------------------------------------------------- elicitation
async def test_client_approval_runs_command_and_is_remembered(tmp_path):
    prompter = Prompter("accept")
    marker = _project(tmp_path) / "ran.txt"
    async with Client(_app(tmp_path), elicitation_handler=prompter) as client:
        cmd = f"echo approved > {marker} && cat {marker}"
        res = await client.call_tool("execute_command", {"command": cmd, "cwd": "."})
        assert "approved" in _text(res)
        assert len(prompter.messages) == 1 and cmd in prompter.messages[0]
        # Same command and cwd: allowed until restart, no second prompt.
        await client.call_tool("execute_command", {"command": cmd, "cwd": "."})
        assert len(prompter.messages) == 1
        # A different command asks again.
        await client.call_tool("execute_command", {"command": "echo other", "cwd": "."})
        assert len(prompter.messages) == 2


async def test_client_decline_does_not_run(tmp_path):
    marker = _project(tmp_path) / "pwned"
    async with Client(_app(tmp_path), elicitation_handler=Prompter("decline")) as client:
        res = await client.call_tool(
            "execute_command", {"command": f"touch {marker}", "cwd": "."}, raise_on_error=False
        )
    assert res.is_error and "denied" in _text(res)
    assert not marker.exists()


async def test_no_answer_times_out(tmp_path):
    marker = _project(tmp_path) / "late"
    async with Client(
        _app(tmp_path, "--approval-timeout", "1"), elicitation_handler=Prompter("accept", delay=5)
    ) as client:
        res = await client.call_tool(
            "execute_command", {"command": f"touch {marker}", "cwd": "."}, raise_on_error=False
        )
    assert res.is_error and "No approval within 1s" in _text(res)
    await anyio.sleep(0.2)
    assert not marker.exists()


async def test_stdio_without_elicitation_refuses_with_guidance(tmp_path):
    async with Client(_app(tmp_path)) as client:  # no elicitation handler, stdio settings
        res = await client.call_tool(
            "execute_command", {"command": "echo hi", "cwd": "."}, raise_on_error=False
        )
    assert res.is_error and "TRUSTED_COMMANDS" in _text(res)


async def test_cwd_outside_allowed_roots_refused_before_asking(tmp_path):
    prompter = Prompter("accept")
    async with Client(_app(tmp_path), elicitation_handler=prompter) as client:
        res = await client.call_tool(
            "execute_command", {"command": "echo hi", "cwd": "/"}, raise_on_error=False
        )
    assert res.is_error and "Directory not allowed" in _text(res)
    assert prompter.messages == []


async def test_no_approval_flag_restores_refusal(tmp_path):
    prompter = Prompter("accept")
    async with Client(_app(tmp_path, "--no-approval"), elicitation_handler=prompter) as client:
        res = await client.call_tool(
            "execute_command", {"command": "echo hi", "cwd": "."}, raise_on_error=False
        )
    assert res.is_error and prompter.messages == []


async def test_trusted_commands_need_no_approval(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[TRUSTED_COMMANDS.t_hi]\ncommand = "echo trusted-hi"\nshell = "bash"\ncwd = "."\n')
    prompter = Prompter("decline")
    app = build_server(load_settings(["-c", str(cfg), "-w", str(_project(tmp_path))]))
    async with Client(app, elicitation_handler=prompter) as client:
        res = await client.call_tool("execute_command", {"command": "t_hi", "cwd": "."})
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
    prompter = Prompter("accept")
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
        import subprocess

        subprocess.run(["tmux", "kill-server"], capture_output=True)


# --------------------------------------------------------------- web page
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_approval_page_flow_and_token_never_reaches_model(tmp_path, caplog):
    port = _free_port()
    app = _app(tmp_path, "-t", "http", "-H", "127.0.0.1", "-P", str(port))
    url = f"http://127.0.0.1:{port}/mcp"
    marker = _project(tmp_path) / "via-page.txt"
    cmd = f"echo page-ok > {marker} && cat {marker}"
    result = {}

    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: app.run_http_async(host="127.0.0.1", port=port, path="/mcp", show_banner=False))
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                await anyio.sleep(0.05)

        async def call():
            async with Client(StreamableHttpTransport(url)) as client:  # no elicitation
                result["res"] = await client.call_tool(
                    "execute_command", {"command": cmd, "cwd": "."}, raise_on_error=False
                )

        with caplog.at_level(logging.WARNING, logger="unified_mcp_server.shell.approvals"):
            tg.start_soon(call)
            link = None
            for _ in range(100):
                for rec in caplog.records:
                    m = re.search(r"(http://\S+/approval/([0-9a-f]+)\?token=([\w-]+))", rec.getMessage())
                    if m:
                        link = m
                if link:
                    break
                await anyio.sleep(0.05)
            assert link, "approval link was not logged"
            page_url, approval_id, token = link.group(1), link.group(2), link.group(3)
            async with httpx.AsyncClient() as http:
                bad = await http.get(f"http://127.0.0.1:{port}/approval/{approval_id}?token=wrong")
                assert bad.status_code == 404
                page = await http.get(page_url)
                assert page.status_code == 200 and "page-ok" in page.text
                bad_post = await http.post(
                    f"http://127.0.0.1:{port}/approval/{approval_id}",
                    data={"token": "wrong", "decision": "allow"},
                )
                assert bad_post.status_code == 404
                ok = await http.post(
                    f"http://127.0.0.1:{port}/approval/{approval_id}",
                    data={"token": token, "decision": "allow"},
                )
                assert "Allowed" in ok.text
            for _ in range(100):
                if "res" in result:
                    break
                await anyio.sleep(0.05)
        tg.cancel_scope.cancel()

    res = result["res"]
    assert not res.is_error and "page-ok" in _text(res)
    assert token not in _text(res) and approval_id not in _text(res)
