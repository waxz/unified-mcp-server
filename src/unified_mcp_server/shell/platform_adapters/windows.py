# Derived from shell-mcp-server (Apache-2.0, https://github.com/waxz/shell-mcp-server);
# modified for unified-mcp-server. See NOTICE and LICENSE-APACHE.
"""Windows command adapter.

Builds the argv for a *trusted* command (cmd / powershell / wsl / bash), which
runs directly on the host. Windows has no built-in sandbox (bubblewrap/docker
need a Linux host), so an untrusted command is governed by the same
**approve/decline** flow as on Linux: the user approves it first (and it then
arrives here trusted), or it's refused. This builder therefore only ever sees
trusted commands; an untrusted one reaching it is a bug upstream, so it fails
closed rather than run on the host.

The working directory is set when the process is spawned (the executor passes
``cwd=``), so this builder never prepends ``cd '<cwd>' && <command>``. That
injected *bash* syntax into whatever shell was selected, and on PowerShell --
which is what ``shell="bash"`` maps to on Windows, and what strict mode coerces
untrusted commands to -- ``&&`` is not a statement separator, so every command
failed with "The token '&&' is not a valid statement separator in this
version." Each shell now just receives the command, and the real directory
comes from the spawn cwd (shell-agnostic, no quoting/escaping of the path).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def build_windows_shell_command(
    shell: str,
    shell_path: str,
    command: str,
    cwd: str,
    trusted: bool = False,
) -> list[str]:
    """Build a Windows shell invocation argv for a trusted command.

    Dispatch is by the real program at ``shell_path`` (not just the logical
    ``shell`` name), so the Windows alias ``shell="bash" -> powershell.exe`` and
    any explicitly configured shell both launch correctly.
    """
    logger.debug("build_windows_shell_command shell=%s path=%s trusted=%s cwd=%s",
                 shell, shell_path, trusted, cwd)

    if not trusted:
        raise RuntimeError(
            f"Refusing an untrusted '{shell}' command on Windows. Windows has no sandbox, "
            "so untrusted commands go through approve/decline: let the user approve it, or "
            "add it to TRUSTED_COMMANDS."
        )

    prog = Path(shell_path).name.lower()

    # WSL is a separate Linux environment: hand it the Windows cwd to translate
    # (its own filesystem view differs), then run the command under bash.
    if shell == "wsl" or prog.startswith("wsl"):
        return [shell_path, "--cd", cwd, "bash", "-c", command]

    # cmd.exe: /d /s /c. /d skips AutoRun; /s makes quote handling deterministic
    # -- cmd strips exactly the outer pair of quotes that list2cmdline adds and
    # takes the rest verbatim. Plain /c instead uses a heuristic ("is the quoted
    # string an executable?") that, on a nested/quoted command line such as
    # `cmd /c echo hi`, leaves a stray trailing quote in the output. /s avoids it.
    if shell == "cmd" or prog in ("cmd", "cmd.exe"):
        return [shell_path, "/d", "/s", "/c", command]

    # PowerShell / pwsh -- also what shell="bash" maps to on Windows (there is
    # no system bash), and what strict mode coerces untrusted commands to.
    if prog.startswith(("powershell", "pwsh")):
        return [shell_path, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]

    # A real POSIX shell configured explicitly (e.g. Git bash, busybox): bash -c.
    return [shell_path, "-c", command]
