"""Self-update: install detection, server discovery, in-place restart."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from unified_mcp_server import update

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="SIGHUP/execv are POSIX")


class FakeDist:
    def __init__(self, direct_url: dict | None):
        self.direct_url = direct_url

    def read_text(self, name):
        return json.dumps(self.direct_url) if self.direct_url is not None else None


# ------------------------------------------------------------ install kind
def test_detect_uv_tool_install_by_receipt(tmp_path):
    """Custom UV_TOOL_DIR paths have no "uv" in them; the receipt file is reliable."""
    env = tmp_path / "anywhere" / "unified-mcp-server"
    env.mkdir(parents=True)
    (env / "uv-receipt.toml").write_text("[tool]\n")
    assert update.detect_install(str(env), FakeDist({"url": "https://x"})).kind == "uv-tool"


def test_detect_pip_install(tmp_path):
    assert update.detect_install(str(tmp_path), FakeDist(None)).kind == "pip"


def test_detect_editable_install_refuses_update():
    m = update.detect_install("/x", FakeDist({"url": "file:///src/repo", "dir_info": {"editable": True}}))
    assert m.kind == "editable"
    with pytest.raises(RuntimeError, match="git pull"):
        update.update_command(m, "main")


def test_update_commands():
    uv_cmd = update.update_command(update.InstallMethod("uv-tool"), "v1.2")
    assert uv_cmd[1:4] == ["tool", "install", "--reinstall"] and "--compile-bytecode" in uv_cmd
    assert uv_cmd[-1] == "unified-mcp-server @ https://github.com/waxz/unified-mcp-server/archive/v1.2.zip"
    pip_cmd = update.update_command(update.InstallMethod("pip"), "main")
    assert pip_cmd[-1].endswith("/archive/main.zip") and "install" in pip_cmd


# --------------------------------------------------------- finding servers
def _fake_proc(tmp_path: Path, procs: dict[int, list[str]]) -> Path:
    root = tmp_path / "proc"
    for pid, args in procs.items():
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
    (root / "self").mkdir(parents=True, exist_ok=True)
    return root


def test_find_servers_filters(tmp_path):
    exe = "/home/me/.local/bin/unified-mcp-server"
    py = "/home/me/.local/share/uv/tools/unified-mcp-server/bin/python3"
    proc = _fake_proc(tmp_path, {
        101: [py, exe, "-w", "/home/me/dev", "-t", "http", "-P", "7000"],  # yes
        102: [py, exe, "-c", "cfg.toml"],                                   # stdio: no
        103: [py, exe, "--update"],                                         # the updater: no
        104: ["/usr/bin/python3", "-m", "unified_mcp_server", "--transport=http"],  # yes
        105: ["/usr/bin/vim", "unified-mcp-server.md", "-t", "http"],       # not us
    })
    found = dict(update.find_servers(proc, uid=os.getuid()))
    assert sorted(found) == [101, 104]


def test_find_servers_ignores_other_users(tmp_path):
    proc = _fake_proc(tmp_path, {201: ["/x/python", "/x/unified-mcp-server", "-t", "http"]})
    assert update.find_servers(proc, uid=os.getuid() + 12345) == []


# ------------------------------------------------------------- run_update
@posix_only
def test_run_update_signals_servers(tmp_path, monkeypatch):
    flag = tmp_path / "got-hup"
    dummy = subprocess.Popen([
        sys.executable, "-c",
        "import signal, sys, time\n"
        f"signal.signal(signal.SIGHUP, lambda *a: (open({str(flag)!r}, 'w').write('hup'), sys.exit(0)))\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n",
    ], stdout=subprocess.PIPE, text=True)
    assert dummy.stdout.readline().strip() == "ready"
    monkeypatch.setattr(update, "detect_install", lambda: update.InstallMethod("uv-tool"))
    monkeypatch.setattr(update, "update_command", lambda m, ref: [sys.executable, "-c", "pass"])
    monkeypatch.setattr(update, "find_servers", lambda: [(dummy.pid, ["x", "-t", "http"])])
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (update._marker_dir()).mkdir(parents=True)
    (update._marker_dir() / str(dummy.pid)).write_text("x")
    assert update.run_update("main") == 0
    dummy.wait(timeout=10)
    assert flag.read_text() == "hup"


def test_run_update_failure_restarts_nothing(monkeypatch):
    monkeypatch.setattr(update, "detect_install", lambda: update.InstallMethod("uv-tool"))
    monkeypatch.setattr(update, "update_command", lambda m, ref: [sys.executable, "-c", "raise SystemExit(3)"])
    called = []
    monkeypatch.setattr(update, "find_servers", lambda: called.append(1) or [])
    assert update.run_update("main") == 3 and called == []


# ------------------------------------------------ in-place restart (real)
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _tools(port: int) -> int:
    async with Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp")) as c:
        return len(await c.list_tools())


@posix_only
async def test_sighup_restarts_server_in_place(tmp_path):
    exe = shutil.which("unified-mcp-server") or str(Path(sys.executable).parent / "unified-mcp-server")
    project = tmp_path / "p"
    project.mkdir()
    port = _free_port()
    log = (tmp_path / "server.log").open("w")
    proc = subprocess.Popen(
        [exe, "-c", str(tmp_path / "none.toml"), "-w", str(project), "-t", "http", "-H", "127.0.0.1", "-P", str(port)],
        stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        async def wait_up():
            for _ in range(100):
                try:
                    return await _tools(port)
                except Exception:
                    await anyio.sleep(0.1)
            raise AssertionError("server did not come up")

        assert await wait_up() > 50
        assert update._is_restartable(proc.pid)  # registered itself
        os.kill(proc.pid, signal.SIGHUP)
        await anyio.sleep(update.RESTART_DELAY_S + 0.5)
        assert proc.poll() is None  # same process, still running
        assert await wait_up() > 50  # and serving again after the exec
        text = (tmp_path / "server.log").read_text()
        assert "restarting in" in text and "Restarting in place" in text
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()


def test_version_flag(capsys):
    from unified_mcp_server.server import main

    main(["--version"])
    assert capsys.readouterr().out.strip() == update.installed_version()


@posix_only
def test_run_update_never_signals_servers_without_restart_support(tmp_path, monkeypatch, capsys):
    """An older server has no SIGHUP handler: SIGHUP would kill it, not restart it."""
    old = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                           stdout=subprocess.PIPE, text=True)
    assert old.stdout.readline().strip() == "ready"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))  # no marker for it
    monkeypatch.setattr(update, "detect_install", lambda: update.InstallMethod("uv-tool"))
    monkeypatch.setattr(update, "update_command", lambda m, ref: [sys.executable, "-c", "pass"])
    monkeypatch.setattr(update, "find_servers", lambda: [(old.pid, ["x", "-t", "http"])])
    try:
        assert update.run_update("main") == 0
        time.sleep(0.5)
        assert old.poll() is None, "older server was killed"
        assert "older version" in capsys.readouterr().err
    finally:
        old.kill()
