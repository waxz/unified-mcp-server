# Derived from shell-mcp-server (Apache-2.0, https://github.com/waxz/shell-mcp-server);
# modified for unified-mcp-server. See NOTICE and LICENSE-APACHE.
import mcp.types as types
from fastmcp.exceptions import ToolError

from .. import config

from .models import (
    ExecutionResult, ProcessRecord, ExecutionRequest,
    ExecuteCommandInput,
    NameInput,
    PidInput,
    TmuxExecuteInput,
    TmuxGetOutputInput,
    TmuxListInput,
    TmuxSessionInput,
)


def format_shell_output(result: ExecutionResult) -> str:
    """Plain-text view of a command's result: stdout, [stderr], [exit code]."""
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout.rstrip("\n"))
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr.rstrip()}")
    parts.append(f"[exit code: {result.exit_code}]")
    return "\n\n".join(parts)


def create_shell_result(result: ExecutionResult) -> types.TextContent:
    """Successful result as text; a non-zero exit becomes a readable tool error.

    The timeout note is already in stderr (added by the executor).
    """
    text = format_shell_output(result)
    if result.exit_code != 0:
        raise ToolError(text)
    return types.TextContent(type="text", text=text)

def create_str_result(result:str) -> types.TextContent:
    return types.TextContent(type="text", text=result)

