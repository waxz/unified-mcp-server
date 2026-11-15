"""Sandboxing of untrusted shell commands (bubblewrap / docker / drun / none)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from unified_mcp_server import config
from unified_mcp_server.config import load_settings
from unified_mcp_server.shell import sandbox

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox")


def _settings(tmp_path: Path, body: str = ""):
    cfg = tmp_path / "c.toml"
    cfg.write_text(body)
    return load_settings(["-c", str(cfg), "-w", str(tmp_path)])


# --------------------------------------------------------------- argv builders
def test_bubblewrap_argv_isolates_and_passes_command_as_argv():
    argv = sandbox.build_bubblewrap_argv(
        "/bin/bash", "rm -rf /", "/home/me/proj/sub", "/home/me/proj", allow_network=False
    )
    assert argv[0] == "bwrap"
    assert argv[:3] == ["bwrap", "--ro-bind", "/"]  # whole system read-only
    assert "--bind" in argv and argv[argv.index("--bind") + 1] == "/home/me/proj"  # project rw
    assert argv[argv.index("--chdir") + 1] == "/home/me/proj/sub"
    assert "--unshare-all" in argv
    assert "--share-net" not in argv  # network cut
    # The command is one argv element, never interpolated into a shell string.
    assert argv[-3:] == ["/bin/bash", "-c", "rm -rf /"]


def test_bubblewrap_network_toggle():
    on = sandbox.build_bubblewrap_argv("/bin/bash", "x", "/w", "/w", allow_network=True)
    assert "--share-net" in on


def test_docker_argv_shape():
    argv = sandbox.build_docker_argv(
        "/bin/bash", "npm i", "/w/app", "/w", "node:22-slim",
        allow_network=False, cpus="2", memory="1g", uid=1000, gid=1000,
    )
    assert argv[:4] == ["docker", "run", "--rm", "-i"]
    assert argv[argv.index("--network") + 1] == "none"
    assert "no-new-privileges" in argv
    assert argv[argv.index("-v") + 1] == "/w:/w"
    assert argv[argv.index("-w") + 1] == "/w/app"
    assert argv[argv.index("--user") + 1] == "1000:1000"
    assert argv[argv.index("--cpus") + 1] == "2" and argv[argv.index("--memory") + 1] == "1g"
    assert argv[-4:] == ["node:22-slim", "/bin/bash", "-c", "npm i"]


# ------------------------------------------------------------ mode resolution
def test_mode_default_is_none(tmp_path):
    s = _settings(tmp_path)
    assert sandbox.effective_mode(s) == sandbox.NONE and not sandbox.is_active(s)


def test_mode_drun_back_compat(tmp_path):
    s = _settings(tmp_path)
    s.DOCKER_SANDBOX_WORKDIR = "/app"  # old way of turning the sandbox on
    assert sandbox.effective_mode(s) == sandbox.DRUN and sandbox.is_active(s)


def test_explicit_sandbox_overrides(tmp_path):
    s = _settings(tmp_path, 'SANDBOX = "bubblewrap"\n')
    assert sandbox.effective_mode(s) == sandbox.BUBBLEWRAP and sandbox.is_active(s)


def test_invalid_sandbox_rejected(tmp_path):
    with pytest.raises(ValueError):
        _settings(tmp_path, 'SANDBOX = "chroot-jail"\n')


# --------------------------------------------------------- fail-closed validate
def test_validate_none_and_in_docker_are_fine(tmp_path):
    sandbox.validate(_settings(tmp_path))  # none: no-op


def test_validate_missing_tool_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _b: None)
    for mode in ("bubblewrap", "docker"):
        s = _settings(tmp_path, f'SANDBOX = "{mode}"\n')
        with pytest.raises(ValueError, match="isn't installed|needs"):
            sandbox.validate(s)


def test_validate_bubblewrap_non_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _b: "/usr/bin/bwrap")
    s = _settings(tmp_path, 'SANDBOX = "bubblewrap"\n')
    s.PLATFORM = "windows"
    with pytest.raises(ValueError, match="Linux-only"):
        sandbox.validate(s)


def test_validate_needs_host_root(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _b: "/usr/bin/bwrap")
    s = _settings(tmp_path, 'SANDBOX = "bubblewrap"\n')
    s.DOCKER_SANDBOX_HOST_ROOT = None
    with pytest.raises(ValueError, match="project directory"):
        sandbox.validate(s)


# ------------------------------------------------- executor uses the sandbox
@posix_only
def test_executor_builds_bubblewrap_argv(tmp_path, monkeypatch):
    from unified_mcp_server.shell.executor import _build_shell_command

    config.SETTINGS = _settings(tmp_path, 'SANDBOX = "bubblewrap"\n')
    argv = _build_shell_command("echo hi", "bash", str(tmp_path), trusted=False)
    assert argv[0] == "bwrap" and argv[-3:] == [config.SETTINGS.ALLOWED_SHELLS["bash"], "-c", "echo hi"]


@posix_only
def test_executor_refuses_untrusted_when_no_sandbox(tmp_path):
    from unified_mcp_server.shell.executor import _build_shell_command

    config.SETTINGS = _settings(tmp_path)  # SANDBOX none
    with pytest.raises(RuntimeError, match="Refusing to run an untrusted"):
        _build_shell_command("echo hi", "bash", str(tmp_path), trusted=False)


# ------------------------------------------------------ windows adapter fixes
def test_windows_adapter_no_crash_on_none_roots():
    from unified_mcp_server.shell.platform_adapters.windows import build_windows_shell_command

    # host_root / work_dir None used to crash with 'NoneType has no attribute replace'.
    argv = build_windows_shell_command(
        shell="bash", shell_path="bash", command="echo hi", cwd=".",
        trusted=False, work_dir=None, host_root=None,
    )
    assert argv and "powershell.exe" in argv[0]


def test_windows_refuses_untrusted_cmd_and_powershell():
    from unified_mcp_server.shell.platform_adapters.windows import build_windows_shell_command

    for shell in ("cmd", "powershell"):
        with pytest.raises(RuntimeError, match="can't be sandboxed"):
            build_windows_shell_command(
                shell=shell, shell_path=f"{shell}.exe", command="whoami", cwd=".", trusted=False,
            )
    # Trusted ones are still allowed.
    assert build_windows_shell_command(
        shell="cmd", shell_path="cmd.exe", command="dir", cwd=".", trusted=True,
    )[:2] == ["cmd.exe", "/c"]


# ---------------------------------------- real bubblewrap end-to-end (if present)
def _bwrap_works() -> bool:
    if shutil.which("bwrap") is None:
        return False
    try:
        r = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--unshare-all", "--", "/bin/true"],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(not _bwrap_works(), reason="bubblewrap not usable on this host")
async def test_bubblewrap_confines_filesystem(tmp_path):
    """The real sandbox: the project dir is writable, the rest of / is not."""
    from fastmcp import Client
    from unified_mcp_server.server import build_server

    project = tmp_path / "proj"
    project.mkdir()
    cfg = tmp_path / "c.toml"
    cfg.write_text('SANDBOX = "bubblewrap"\nSANDBOX_NETWORK = "none"\n')
    app = build_server(load_settings(["-c", str(cfg), "-w", str(project)]))
    probe = Path(f"/etc/unified_mcp_sandbox_probe_{os.getpid()}")
    async with Client(app) as client:
        # Writing inside the project dir works (it's bind-mounted read-write).
        ok = await client.call_tool(
            "execute_command", {"command": "echo sandboxed > inside.txt && cat inside.txt", "cwd": "."},
            raise_on_error=False,
        )
        assert not ok.is_error, "".join(c.text for c in ok.content)
        assert "sandboxed" in "".join(c.text for c in ok.content)
        assert (project / "inside.txt").read_text().strip() == "sandboxed"
        # Writing onto the read-only root (/etc) fails inside the sandbox.
        bad = await client.call_tool(
            "execute_command", {"command": f"echo x > {probe}", "cwd": "."}, raise_on_error=False
        )
        assert bad.is_error
    assert not probe.exists()  # nothing escaped to the real host
