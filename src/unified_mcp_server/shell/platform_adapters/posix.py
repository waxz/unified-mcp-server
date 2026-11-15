# Derived from shell-mcp-server (Apache-2.0, https://github.com/waxz/shell-mcp-server);
# modified for unified-mcp-server. See NOTICE and LICENSE-APACHE.
"""POSIX command adapter.

Builds the argv for a *trusted* command, which runs directly on the host.
Untrusted commands never reach here: the executor hands them to the sandbox
(shell/sandbox.py), which builds its own argv, or refuses them.
"""

from __future__ import annotations

import logging
import shlex

logger = logging.getLogger(__name__)


def build_posix_shell_command(shell_path: str, command: str, cwd: str) -> list[str]:
    """``shell -c '(cd <cwd> && <command>)'`` for a trusted host command."""
    if cwd in ("", "."):
        wrapped = command
    else:
        wrapped = f"( cd {shlex.quote(cwd)} && {command} )"
    logger.debug("build_posix_shell_command: %s", wrapped)
    return [shell_path, "-c", wrapped]
