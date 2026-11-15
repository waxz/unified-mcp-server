"""Security guards: the server must *catch* a dangerous tool call and, crucially,
never let its side effect happen.

Two kinds of danger are exercised end-to-end through a real MCP client (so the
checks run through the same path a client hits), and asserted both ways -- the
call is an error *and* nothing escaped / nothing ran:

* file tools reaching **outside the working directory** -- ``..`` traversal, an
  absolute path, a symlink pointing out of the project, and a protected file;
* shell tools running a **dangerous / untrusted command** -- it must be refused
  (no sandbox, no approval here) and must not have executed.

These run on POSIX (CI is Linux); the guards themselves are cross-platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastmcp import Client

from unified_mcp_server.config import load_settings
from unified_mcp_server.server import build_server

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="symlink/shell fixtures assume POSIX"
)


def _app(project: Path, cfg: Path, body: str = ""):
    cfg.write_text(body)
    return build_server(load_settings(["-c", str(cfg), "-w", str(project)]))


async def _err(client: Client, name: str, args: dict) -> str:
    res = await client.call_tool(name, args, raise_on_error=False)
    assert res.is_error, f"{name}({args}) should have been refused but succeeded"
    return "".join(getattr(c, "text", "") for c in res.content)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    return p


@pytest.fixture
async def client(tmp_path: Path, project: Path):
    app = _app(project, tmp_path / "config.toml")
    async with Client(app) as c:
        yield c


# --------------------------------------------------------- out of working dir
class TestFileBoundary:
    async def test_parent_traversal_is_refused(self, client, project, tmp_path):
        secret = tmp_path / "outside.txt"  # a sibling of the project dir
        secret.write_text("TOP SECRET")
        msg = await _err(client, "read_file", {"path": "../outside.txt"})
        assert "traversal" in msg.lower() or "not allowed" in msg.lower()
        assert "TOP SECRET" not in msg  # the content never leaked

    async def test_absolute_path_outside_project_is_refused(self, client):
        # An absolute path that resolves outside the project root is rejected.
        await _err(client, "read_file", {"path": "/etc/hostname"})

    async def test_write_cannot_escape_the_project(self, client, tmp_path):
        await _err(client, "write_file", {"path": "../escaped.txt", "content": "x"})
        assert not (tmp_path / "escaped.txt").exists()  # nothing was written outside

    async def test_listing_parent_is_refused(self, client):
        await _err(client, "list_files", {"path": ".."})

    async def test_symlink_out_of_project_is_refused(self, client, project, tmp_path):
        # A real escape attempt: a symlink inside the project that points out of
        # it. resolve() follows it, so the boundary check must still catch it.
        outside = tmp_path / "outside_target.txt"
        outside.write_text("secret-via-symlink")
        (project / "link.txt").symlink_to(outside)
        msg = await _err(client, "read_file", {"path": "link.txt"})
        assert "secret-via-symlink" not in msg

    async def test_protected_file_is_refused(self, tmp_path, project):
        (project / "secret.env").write_text("API_KEYS=hunter2")
        app = _app(project, tmp_path / "config.toml", 'PROTECTED_PATHS = ["secret.env"]\n')
        async with Client(app) as client:
            msg = await _err(client, "read_file", {"path": "secret.env"})
            assert "hunter2" not in msg
            # ...and it can't be overwritten either.
            await _err(client, "write_file", {"path": "secret.env", "content": "x"})
            assert (project / "secret.env").read_text() == "API_KEYS=hunter2"

    async def test_the_config_file_is_always_protected(self, tmp_path, project):
        # The config holds API_KEYS; it's protected even if inside the roots.
        cfg = project / "config.toml"
        app = _app(project, cfg, 'API_KEYS = "super-secret-key"\n')
        async with Client(app) as client:
            msg = await _err(client, "read_file", {"path": "config.toml"})
            assert "super-secret-key" not in msg


# ------------------------------------------------------- dangerous commands
class TestDangerousCommands:
    async def test_untrusted_command_is_refused_and_does_not_run(self, client, project):
        # No sandbox and no way to approve here (no elicitation, stdio): the
        # command must be refused AND must not have executed.
        msg = await _err(client, "execute_command", {"command": "touch pwned", "cwd": "."})
        assert "TRUSTED_COMMANDS" in msg or "approve" in msg.lower()
        assert not (project / "pwned").exists()

    async def test_destructive_command_cannot_delete_files(self, client, project):
        keep = project / "keep.txt"
        keep.write_text("do not delete")
        await _err(client, "execute_command", {"command": "rm -rf ./*", "cwd": "."})
        assert keep.exists() and keep.read_text() == "do not delete"

    async def test_command_cwd_outside_roots_is_refused_before_asking(self, client):
        msg = await _err(client, "execute_command", {"command": "ls", "cwd": "/etc"})
        assert "not allowed" in msg.lower() or "directory" in msg.lower()

    async def test_no_approval_mode_refuses_without_running(self, tmp_path, project):
        cfg = tmp_path / "c.toml"
        cfg.write_text("")
        app = build_server(load_settings(["-c", str(cfg), "-w", str(project), "--no-approval"]))
        async with Client(app) as client:
            await _err(client, "execute_command", {"command": "touch sneaky", "cwd": "."})
            assert not (project / "sneaky").exists()
