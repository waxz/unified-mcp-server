"""Regression tests for the bash-mcp test-report bugs (file-edit side).

Covered:
  #1 apply_patch / preview corrupted content (doubled blank lines, and -- worse
     -- the preview path rebuilt "new content" from a context-only diff, which
     would drop every line outside a hunk);
  #2 get_file_info returned a *relative* path in the `absolute_path` field;
  #4 patch_file left an orphan .backup_* on disk when nothing matched;
  #5 read_file with an out-of-range line window returned empty content silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from unified_mcp_server.config import load_settings
from unified_mcp_server.server import build_server


@pytest.fixture
async def client(tmp_path: Path):
    project = tmp_path / "p"
    project.mkdir()
    app = build_server(load_settings(["-c", str(tmp_path / "none.toml"), "-w", str(project)]))
    async with Client(app) as c:
        yield c, project


async def _data(client: Client, name: str, args: dict):
    res = await client.call_tool(name, args, raise_on_error=False)
    import json
    text = "".join(getattr(c, "text", "") for c in res.content)
    try:
        return json.loads(text)
    except ValueError:
        return text


SMALL_PATCH = """*** Begin Patch
*** Update File: patchtest.txt
@@
-alpha
+ALPHA
 beta
*** End Patch
"""


# ---------------------------------------------------------------- #1 corruption
class TestPatchContent:
    async def test_dry_run_new_content_and_diff_not_corrupted(self, client):
        c, project = client
        await _data(c, "write_file", {"path": "patchtest.txt", "content": "alpha\nbeta\ngamma\n"})
        from unified_mcp_server.files import file_patch_tools as fpt

        dry = await fpt.apply_patch(SMALL_PATCH, dry_run=True)
        change = dry["changes"][0]
        # The dry-run carries the exact full post-patch content...
        assert change["new_content"] == "ALPHA\nbeta\ngamma\n"
        # ...and the shown diff has no doubled blank lines.
        assert "\n\n" not in change["diff"].rstrip("\n")
        # The preview entry (what the confirm/apply path stores) matches.
        entries = fpt._build_preview_diff_entries(dry, {change["path"]: "alpha\nbeta\ngamma\n"})
        assert entries[0]["new_content"] == "ALPHA\nbeta\ngamma\n"

    async def test_confirmed_patch_writes_correct_content(self, client):
        c, project = client
        await _data(c, "write_file", {"path": "patchtest.txt", "content": "alpha\nbeta\ngamma\n"})
        # Real review workflow: apply_patch(dry_run=True) -> confirm -> apply.
        res = await c.call_tool("apply_patch", {"patch": SMALL_PATCH, "dry_run": True}, raise_on_error=False)
        sc = getattr(res, "structured_content", None) or getattr(res, "data", None) or {}
        pid = (sc.get("preview_session") or {}).get("preview_id")
        assert pid, sc
        await c.call_tool("confirm_patch_preview", {"preview_id": pid})
        await c.call_tool("apply_confirmed_patch", {"preview_id": pid})
        assert (project / "patchtest.txt").read_text() == "ALPHA\nbeta\ngamma\n"

    async def test_preview_keeps_lines_outside_the_hunk(self, client):
        # The old preview rebuilt content from the (context-only) diff, so for a
        # file larger than the hunk it dropped everything else. Must not happen.
        c, project = client
        big = "".join(f"line{i}\n" for i in range(1, 31))
        await _data(c, "write_file", {"path": "big.txt", "content": big})
        from unified_mcp_server.files import file_patch_tools as fpt

        patch = (
            "*** Begin Patch\n*** Update File: big.txt\n@@\n-line15\n+LINE15\n line16\n*** End Patch\n"
        )
        dry = await fpt.apply_patch(patch, dry_run=True)
        assert dry["changes"][0]["new_content"] == big.replace("line15", "LINE15")


# ------------------------------------------------------------- #2 get_file_info
async def test_get_file_info_absolute_path_is_absolute(client):
    c, project = client
    await _data(c, "write_file", {"path": "a.txt", "content": "x\n"})
    info = await _data(c, "get_file_info", {"path": "a.txt"})
    assert Path(info["absolute_path"]).is_absolute()
    assert info["absolute_path"] != info["relative_path"]
    assert info["relative_path"] in ("a.txt", "./a.txt")


# ---------------------------------------------------------- #4 no orphan backup
async def test_patch_file_no_match_leaves_no_backup(client):
    c, project = client
    await _data(c, "write_file", {"path": "a.txt", "content": "line1\nline2\n"})
    res = await _data(c, "patch_file", {
        "path": "a.txt",
        "patches": [{"find": "NOT_PRESENT", "replace": "x"}],
    })
    assert res["patches_applied"] == 0 and res["success"] is False
    assert res["backup_path"] is None
    assert list(project.glob("a.txt.backup_*")) == []  # nothing left on disk
    assert (project / "a.txt").read_text() == "line1\nline2\n"  # untouched


async def test_patch_file_match_still_backs_up(client):
    c, project = client
    await _data(c, "write_file", {"path": "a.txt", "content": "line1\nline2\n"})
    res = await _data(c, "patch_file", {
        "path": "a.txt",
        "patches": [{"find": "line1", "replace": "LINE1"}],
    })
    assert res["patches_applied"] == 1 and res["backup_path"]
    assert list(project.glob("a.txt.backup_*"))  # a real edit keeps its backup


# ------------------------------------------------- #5 out-of-range read window
async def test_read_file_out_of_range_window_warns(client):
    c, project = client
    await _data(c, "write_file", {"path": "a.txt", "content": "l1\nl2\nl3\n"})
    res = await _data(c, "read_file", {"path": "a.txt", "start_line": 10, "end_line": 20})
    assert res["content"] == ""
    assert res["total_lines"] == 3
    assert "warning" in res and "outside the file" in res["warning"]


async def test_read_file_in_range_window_has_no_warning(client):
    c, project = client
    await _data(c, "write_file", {"path": "a.txt", "content": "l1\nl2\nl3\n"})
    res = await _data(c, "read_file", {"path": "a.txt", "start_line": 2, "end_line": 3})
    assert res["content"] == "l2\nl3\n" and "warning" not in res
