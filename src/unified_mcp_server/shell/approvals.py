"""Interactive approval for shell commands that aren't in TRUSTED_COMMANDS.

When no sandbox is configured, a command that isn't a trusted command can
still run on the host if the person running the server approves it:

* through the MCP client (elicitation), when the client supports it;
* on an approve/deny web page, whose link (with a one-time token) is printed
  only to the server log, never returned to the model, so the model can't
  approve its own request.

The request waits ``APPROVAL_TIMEOUT`` seconds (default 30) for whichever
answers first; no answer is a denial. An approved command (exact command,
shell and working directory) stays approved until the server restarts.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from html import escape

from starlette.requests import Request
from starlette.responses import HTMLResponse

from .. import config
from ..common.models import ExecutionRequest
from .execution_policy import (
    _resolve_absolute_cwd,
    _validate_shell_name,
    _validate_string_cwd,
    _validate_string_input,
    resolve_request,
)

logger = logging.getLogger(__name__)

ApprovalKey = tuple[str, str, str]  # (command, shell, absolute cwd)


class ApprovalDenied(PermissionError):
    """The command was denied, or nobody answered in time."""


@dataclass
class PendingApproval:
    id: str
    token: str
    key: ApprovalKey
    expires_at: float
    decided: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool = False
    via: str = ""

    def decide(self, approved: bool, via: str) -> bool:
        if self.decided.is_set():
            return False
        self.approved = approved
        self.via = via
        self.decided.set()
        return True


class ApprovalStore:
    def __init__(self) -> None:
        self.approved: set[ApprovalKey] = set()
        self.pending: dict[str, PendingApproval] = {}

    def reset(self) -> None:
        self.approved.clear()
        self.pending.clear()

    def is_approved(self, key: ApprovalKey) -> bool:
        return key in self.approved

    def get(self, approval_id: str, token: str) -> PendingApproval | None:
        pending = self.pending.get(approval_id)
        if pending is None or not hmac.compare_digest(pending.token, token or ""):
            return None
        if time.monotonic() > pending.expires_at and not pending.decided.is_set():
            return None
        return pending

    async def require(self, key: ApprovalKey, ctx) -> None:
        """Return if ``key`` is (or gets) approved; raise ApprovalDenied otherwise."""
        if self.is_approved(key):
            return
        settings = config.SETTINGS
        timeout = settings.APPROVAL_TIMEOUT
        pending = PendingApproval(
            id=secrets.token_hex(8),
            token=secrets.token_urlsafe(24),
            key=key,
            expires_at=time.monotonic() + timeout,
        )
        self.pending[pending.id] = pending
        command, shell, cwd = key

        channels: list[str] = []
        page_url = approval_page_url(pending)
        if page_url:
            channels.append("the approval page")
            # Only on the server's log: never in the tool result the model sees.
            logger.warning(
                "Approval needed (%ss) to run on this machine: %r in %s (shell %s). "
                "Open %s to approve or deny.",
                timeout, command, cwd, shell, page_url,
            )
        elicit_task = None
        if _client_supports_elicitation(ctx):
            channels.append("your MCP client")
            elicit_task = asyncio.create_task(_elicit(ctx, pending))

        try:
            if not channels:
                raise ApprovalDenied(
                    f"'{command}' is not a trusted command and can't be approved here: the "
                    "client doesn't support approval prompts (elicitation) and the stdio "
                    "transport has no approval page. Add it to TRUSTED_COMMANDS, or run the "
                    "server with -t http."
                )
            try:
                await asyncio.wait_for(pending.decided.wait(), timeout)
            except asyncio.TimeoutError:
                pending.decide(False, "timeout")
                raise ApprovalDenied(
                    f"No approval within {timeout}s to run '{command}' (asked via "
                    f"{' and '.join(channels)}). Ask the user to approve it, then call again."
                ) from None
            if not pending.approved:
                raise ApprovalDenied(f"The user denied running '{command}'.")
            self.approved.add(key)
            logger.info("Approved until restart (via %s): %r in %s", pending.via, command, cwd)
        finally:
            if elicit_task is not None and not elicit_task.done():
                elicit_task.cancel()
            self.pending.pop(pending.id, None)


APPROVALS = ApprovalStore()


def _client_supports_elicitation(ctx) -> bool:
    try:
        params = ctx.session.client_params
        return bool(params and params.capabilities and params.capabilities.elicitation)
    except Exception:
        return False


async def _elicit(ctx, pending: PendingApproval) -> None:
    command, shell, cwd = pending.key
    message = (
        "The model wants to run a command that isn't in TRUSTED_COMMANDS, on the "
        f"machine running unified-mcp-server:\n\n  {command}\n\nin {cwd} (shell: {shell}).\n\n"
        "Allow it? If allowed, this exact command stays allowed until the server restarts."
    )
    try:
        result = await ctx.elicit(message, response_type=None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # client failed to show the prompt: leave the page to decide
        logger.info("Approval prompt via the MCP client failed: %s", exc)
        return
    pending.decide(getattr(result, "action", "") == "accept", "client")


def approval_page_url(pending: PendingApproval) -> str | None:
    settings = config.SETTINGS
    if settings.TRANSPORT != "http":
        return None
    if settings.PUBLIC_URL:
        base = settings.PUBLIC_URL.rstrip("/")
    else:
        host = "localhost" if settings.HOST in {"0.0.0.0", "::"} else settings.HOST
        base = f"http://{host}:{settings.PORT}"
    return f"{base}/approval/{pending.id}?token={pending.token}"


async def authorize(command: str, cwd: str, shell: str, ctx) -> ExecutionRequest | None:
    """Decide how a shell command may run.

    Returns None when the existing policy applies unchanged (trusted command,
    sandbox configured, approvals off, or running inside the container).
    Otherwise asks for approval and returns a host request, or raises
    ApprovalDenied.
    """
    settings = config.SETTINGS
    if (
        not settings.APPROVE_UNTRUSTED
        or settings.IS_IN_DOCKER
        or settings.DOCKER_SANDBOX_WORKDIR
        or command in settings.TRUSTED_COMMANDS
    ):
        return None
    _validate_string_input(command, "command")
    _validate_string_cwd(cwd, "cwd")
    _validate_shell_name(shell)
    if settings.SAFETY_MODE == "strict":
        shell = "bash"
    if shell not in settings.ALLOWED_SHELLS:
        available = ", ".join(sorted(settings.ALLOWED_SHELLS))
        raise ValueError(f"Shell '{shell}' is not allowed. Available: {available}")
    # The working directory must still be inside the allowed host roots.
    host_cwd = _resolve_absolute_cwd(cwd, settings, is_trusted=True)
    await APPROVALS.require((command, shell, host_cwd), ctx)
    return ExecutionRequest(command=command, cwd=host_cwd, shell=shell, trusted=True)


# --------------------------------------------------------------- web page
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Approve command</title>
<style>
:root {{ --bg:#f6f6f4; --fg:#1d1d1b; --card:#fff; --line:#d9d9d4; --mono:#f0f0ec; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#18181a; --fg:#ececea; --card:#232326; --line:#3a3a3e; --mono:#2c2c30; }} }}
body {{ margin:0; font:16px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }}
main {{ max-width:720px; margin:0 auto; padding:24px 16px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:20px; }}
pre {{ background:var(--mono); padding:12px; border-radius:6px; overflow-x:auto;
       white-space:pre-wrap; word-break:break-all; }}
button {{ font:inherit; padding:10px 18px; border-radius:6px; border:0; cursor:pointer; color:#fff; }}
.allow {{ background:#1f6d42; }} .deny {{ background:#8f2d2d; margin-left:8px; }}
</style></head><body><main><div class="card">{body}</div></main></body></html>"""


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(body=body), status_code=status)


def register_approval_routes(app) -> None:
    @app.custom_route("/approval/{approval_id}", methods=["GET"], include_in_schema=False)
    async def approval_page(request: Request) -> HTMLResponse:
        pending = APPROVALS.get(request.path_params["approval_id"], request.query_params.get("token", ""))
        if pending is None:
            return _page("<h1>Request not found</h1><p>It expired, was answered, or the link is wrong.</p>", 404)
        command, shell, cwd = pending.key
        if pending.decided.is_set():
            state = "approved" if pending.approved else "denied"
            return _page(f"<h1>Already {state}</h1><pre>{escape(command)}</pre>")
        left = max(0, int(pending.expires_at - time.monotonic()))
        return _page(
            "<h1>Run this command?</h1>"
            "<p>The model asked unified-mcp-server to run a command that isn't in "
            "TRUSTED_COMMANDS, on this machine:</p>"
            f"<pre>{escape(command)}</pre>"
            f"<p>in <code>{escape(cwd)}</code>, shell <code>{escape(shell)}</code>. "
            "If you allow it, this exact command stays allowed until the server restarts. "
            f"This request expires in about {left} s.</p>"
            f'<form method="post" action="/approval/{escape(pending.id)}">'
            f'<input type="hidden" name="token" value="{escape(pending.token)}">'
            '<button class="allow" name="decision" value="allow">Allow</button>'
            '<button class="deny" name="decision" value="deny">Deny</button></form>'
        )

    @app.custom_route("/approval/{approval_id}", methods=["POST"], include_in_schema=False)
    async def approval_decide(request: Request) -> HTMLResponse:
        form = await request.form()
        pending = APPROVALS.get(request.path_params["approval_id"], str(form.get("token", "")))
        if pending is None:
            return _page("<h1>Request not found</h1><p>It expired, was answered, or the link is wrong.</p>", 404)
        allow = form.get("decision") == "allow"
        if not pending.decide(allow, "page"):
            return _page("<h1>Already answered</h1>")
        return _page(f"<h1>{'Allowed' if allow else 'Denied'}</h1><p>You can close this page.</p>")


__all__ = [
    "APPROVALS",
    "ApprovalDenied",
    "authorize",
    "register_approval_routes",
    "resolve_request",
]
