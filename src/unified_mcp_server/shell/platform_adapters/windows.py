# Derived from shell-mcp-server (Apache-2.0, https://github.com/waxz/shell-mcp-server);
# modified for unified-mcp-server. See NOTICE and LICENSE-APACHE.
"""Windows command adapter.

Builds the argv for a *trusted* command (cmd / powershell / wsl / bash), which
runs directly on the host. Windows has no built-in sandbox (bubblewrap is
Linux-only), so an *untrusted* command can't be confined here and is refused --
use the approval flow (which runs it only after you allow it), a Linux host
with SANDBOX='bubblewrap'/'docker', or TRUSTED_COMMANDS.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def build_windows_shell_command(
    shell: str,
    shell_path: str,
    command: str,
    cwd: str,
    trusted: bool = False,
) -> list[str]:
    """Build a Windows shell invocation argv for a trusted command."""
    logger.debug("build_windows_shell_command shell=%s trusted=%s cwd=%s", shell, trusted, cwd)

    if not trusted:
        raise RuntimeError(
            f"Refusing an untrusted '{shell}' command on Windows: it can't be sandboxed "
            "here. Approve it, add it to TRUSTED_COMMANDS, or run on a Linux host with a "
            "SANDBOX set."
        )

    if shell == "wsl":
        return [shell_path, "--cd", cwd, "bash", "-c", command]
    if shell == "cmd":
        return [shell_path, "/c", command]
    if shell == "bash":
        inner = command if cwd in ("", ".") else f"( cd '{cwd}' && {command} )"
        return [shell_path, "-c", inner]
    # powershell / pwsh
    return [shell_path, "-ExecutionPolicy", "Bypass", "-Command", command]
