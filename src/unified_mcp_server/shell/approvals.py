"""Interactive approval for shell commands that aren't in TRUSTED_COMMANDS.

When no sandbox is configured, a command that isn't a trusted command can
still run on the host if the person running the server approves it. They can
answer in three places, whichever is handiest:

* the MCP client's prompt (elicitation), when the client supports it:
  Submit allows once (tick the checkbox to allow until restart), Decline
  denies;
* the approvals dashboard, a live page listing every pending request, whose
  link is printed once in the server log at startup;
* a per-request page, whose link is printed in the log with each request.

Links carry random tokens and are printed only to the server log, never
returned to the model, so the model can't approve its own request. The
dashboard token is regenerated every time the server starts and is never
stored in the config file (which the file tools may be able to read).

Answers: *Allow once*, *Allow until restart* (same command, shell and working
directory run without asking until the server restarts), or *Deny*.

Timing:

* A request stays open for ``APPROVAL_TIMEOUT`` seconds (default 600).
* A tool call waits up to ``APPROVAL_WAIT`` seconds (default 25) for the
  answer, so it doesn't hang. If nobody has answered yet it returns
  "approval pending"; calling again with the same arguments picks up the same
  request and runs as soon as it's approved.
* While the client is showing its own prompt, the call waits for that answer
  (up to the request's lifetime) instead.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import socket
import time
from dataclasses import dataclass, field
from html import escape

from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

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

ONCE, SESSION, DENY = "once", "session", "deny"
_LABELS = {ONCE: "Allow once", SESSION: "Allow until restart", DENY: "Deny"}


class ApprovalDenied(PermissionError):
    """The user denied the command, or the request expired unanswered."""


class ApprovalPending(PermissionError):
    """Nobody has answered yet; the request is still open."""


@dataclass
class ApprovalRequest:
    id: str
    token: str
    key: ApprovalKey
    created_at: float
    expires_at: float
    decided: asyncio.Event = field(default_factory=asyncio.Event)
    decision: str | None = None
    via: str = ""

    def decide(self, decision: str, via: str) -> bool:
        if self.decided.is_set() or decision not in _LABELS:
            return False
        self.decision = decision
        self.via = via
        self.decided.set()
        return True

    def remaining(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())


class ApprovalStore:
    def __init__(self) -> None:
        self.approved: dict[ApprovalKey, float] = {}  # key -> approved at (time.time)
        self.requests: dict[str, ApprovalRequest] = {}
        self.dashboard_token = secrets.token_urlsafe(24)

    def reset(self, dashboard_token: str | None = None) -> None:
        """Forget everything. Without a token, a new server gets a fresh one;
        with one (APPROVALS_DASHBOARD_TOKEN), the dashboard link is stable
        across restarts so you can bookmark it."""
        self.approved.clear()
        self.requests.clear()
        self.dashboard_token = dashboard_token or secrets.token_urlsafe(24)

    # ------------------------------------------------------------ lookups
    def _expire(self) -> None:
        # Answered requests expire too: an unused "allow once" must not linger.
        now = time.monotonic()
        for rid, req in list(self.requests.items()):
            if now > req.expires_at:
                del self.requests[rid]

    def _drop(self, key: ApprovalKey) -> None:
        for rid, req in list(self.requests.items()):
            if req.key == key:
                del self.requests[rid]

    def _open_for(self, key: ApprovalKey) -> ApprovalRequest | None:
        self._expire()
        for req in self.requests.values():
            if req.key == key:
                return req
        return None

    def by_token(self, request_id: str, token: str) -> ApprovalRequest | None:
        self._expire()
        req = self.requests.get(request_id)
        if req is None or not hmac.compare_digest(req.token, token or ""):
            return None
        return req

    def dashboard_ok(self, token: str) -> bool:
        return hmac.compare_digest(self.dashboard_token, token or "")

    def pending(self) -> list[ApprovalRequest]:
        self._expire()
        return sorted(
            (r for r in self.requests.values() if not r.decided.is_set()),
            key=lambda r: r.created_at,
        )

    # ---------------------------------------------------------- answers
    def decide(self, req: ApprovalRequest, decision: str, via: str) -> bool:
        if not req.decide(decision, via):
            return False
        if decision == SESSION:
            self.approved[req.key] = time.time()
        command, _shell, cwd = req.key
        logger.info("%s (via %s): %r in %s", _LABELS[decision], via, command, cwd)
        return True

    def revoke(self, key: ApprovalKey) -> bool:
        self._drop(key)  # also forget any answer not yet used
        return self.approved.pop(key, None) is not None

    # --------------------------------------------------------- the gate
    async def require(self, key: ApprovalKey, ctx) -> None:
        """Return when ``key`` may run; raise ApprovalDenied / ApprovalPending."""
        if key in self.approved:
            self._drop(key)  # an "until restart" answer may still be stored
            return
        settings = config.SETTINGS
        command, shell, cwd = key

        req = self._open_for(key)
        if req is not None and req.decided.is_set():
            self._consume(req)  # answered while the model was away
            return
        if req is None:
            req = self._new_request(key, settings.APPROVAL_TIMEOUT)

        elicit = None
        if _client_supports_elicitation(ctx):
            elicit = asyncio.create_task(_elicit(ctx, req, self))
        if elicit is None and not dashboard_url():
            self.requests.pop(req.id, None)
            raise ApprovalDenied(
                f"'{command}' is not a trusted command and can't be approved here: the "
                "client doesn't support approval prompts (elicitation) and the stdio "
                "transport has no approval page. Add it to TRUSTED_COMMANDS, or run the "
                "server with -t http."
            )

        await self._wait(req, elicit, settings.APPROVAL_WAIT)
        if req.decided.is_set():
            self._consume(req)
            return
        if req.remaining() <= 0:
            self.requests.pop(req.id, None)
            raise ApprovalDenied(
                f"Nobody approved '{command}' within {settings.APPROVAL_TIMEOUT}s; the request "
                "expired. Ask the user before trying again."
            )
        raise ApprovalPending(
            f"Waiting for the user to approve '{command}' (in {cwd}). It hasn't run. The request "
            f"stays open for about {int(req.remaining())}s: tell the user it's waiting on the "
            "approvals page or their client's prompt, then call again with exactly the same "
            "arguments; it runs as soon as it's approved."
        )

    def _new_request(self, key: ApprovalKey, timeout: int) -> ApprovalRequest:
        now = time.monotonic()
        req = ApprovalRequest(
            id=secrets.token_hex(8),
            token=secrets.token_urlsafe(24),
            key=key,
            created_at=now,
            expires_at=now + timeout,
        )
        self.requests[req.id] = req
        command, shell, cwd = key
        page = request_url(req)
        if page:
            # Only in the server's log: never in a tool result the model sees.
            logger.warning(
                "\a\nApproval needed to run on this machine: %r in %s (shell %s). Open the "
                "approvals dashboard, or %s  (open for %ss)",
                command, cwd, shell, page, timeout,
            )
        return req

    def _consume(self, req: ApprovalRequest) -> None:
        self.requests.pop(req.id, None)
        if req.decision == DENY:
            raise ApprovalDenied(
                f"The user denied running '{req.key[0]}'. Don't retry it unless they ask."
            )

    async def _wait(self, req: ApprovalRequest, elicit: asyncio.Task | None, page_wait: int) -> None:
        """Wait for an answer: the request's lifetime while the client prompt is
        open, otherwise ``page_wait`` seconds."""
        start = time.monotonic()
        decided = asyncio.create_task(req.decided.wait())
        tasks: set[asyncio.Task] = {decided}
        if elicit is not None:
            tasks.add(elicit)
        try:
            while not req.decided.is_set():
                prompt_open = elicit is not None and not elicit.done()
                limit = req.remaining() if prompt_open else min(
                    req.remaining(), start + page_wait - time.monotonic()
                )
                if limit <= 0:
                    return
                done, _ = await asyncio.wait(tasks, timeout=limit, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    return
                if elicit is not None and elicit in done:
                    tasks.discard(elicit)  # prompt dismissed/failed: fall back to the page
        finally:
            for task in (decided, elicit):
                if task is not None and not task.done():
                    task.cancel()


APPROVALS = ApprovalStore()


def _client_supports_elicitation(ctx) -> bool:
    try:
        params = ctx.session.client_params
        return bool(params and params.capabilities and params.capabilities.elicitation)
    except Exception:
        return False


class _PromptAnswer(BaseModel):
    """Form shown by the MCP client: Submit allows, Decline denies.

    A single optional checkbox instead of a choice list: clients render a
    choice as a dropdown (a native picker sheet on phones), which costs an
    extra tap and puts the options far from the buttons.
    """

    remember_until_restart: bool = Field(
        default=False,
        title="Also allow this exact command until the server restarts",
    )


async def _elicit(ctx, req: ApprovalRequest, store: ApprovalStore) -> None:
    command, shell, cwd = req.key
    message = (
        f"Run this command on {socket.gethostname()}?\n\n"
        f"{command}\n\n"
        f"in {cwd} ({shell})\n\n"
        "Submit allows it once. Decline denies it. It isn't in TRUSTED_COMMANDS."
    )
    try:
        result = await ctx.elicit(message, response_type=_PromptAnswer)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # client couldn't show it: leave the dashboard to decide
        logger.info("Approval prompt via the MCP client failed: %s", exc)
        return
    action = getattr(result, "action", "")
    if action == "accept":
        data = getattr(result, "data", None)
        remember = bool(getattr(data, "remember_until_restart", False))
        store.decide(req, SESSION if remember else ONCE, "client")
    elif action == "decline":
        store.decide(req, DENY, "client")
    # "cancel" (dismissed): no answer; the request stays open on the dashboard.


def _base_url() -> str | None:
    settings = config.SETTINGS
    if settings is None or settings.TRANSPORT != "http":
        return None
    if settings.PUBLIC_URL:
        return settings.PUBLIC_URL.rstrip("/")
    host = "localhost" if settings.HOST in {"0.0.0.0", "::"} else settings.HOST
    return f"http://{host}:{settings.PORT}"


def dashboard_url() -> str | None:
    base = _base_url()
    return f"{base}/approvals?key={APPROVALS.dashboard_token}" if base else None


def request_url(req: ApprovalRequest) -> str | None:
    base = _base_url()
    return f"{base}/approvals/{req.id}?token={req.token}" if base else None


async def authorize(command: str, cwd: str, shell: str, ctx) -> ExecutionRequest | None:
    """Decide how a shell command may run.

    Returns None when the existing policy applies unchanged (trusted command,
    sandbox configured, approvals off, or running inside the container).
    Otherwise gets the user's approval and returns a host request, or raises
    ApprovalDenied / ApprovalPending.
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


# ------------------------------------------------------------------ pages
_STYLE = """
:root { --bg:#f6f6f4; --fg:#1d1d1b; --muted:#65655f; --card:#fff; --line:#d9d9d4; --mono:#f0f0ec;
        --ok:#1f6d42; --ok2:#2d5f8a; --no:#8f2d2d; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#18181a; --fg:#ececea; --muted:#a3a39d; --card:#232326; --line:#3a3a3e; --mono:#2c2c30; } }
* { box-sizing:border-box; }
body { margin:0; font:16px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
main { max-width:760px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:1.4rem; margin:0 0 4px; } h2 { font-size:1.05rem; margin:28px 0 8px; }
.muted { color:var(--muted); font-size:.9rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:16px; margin:12px 0; }
pre { background:var(--mono); padding:10px 12px; border-radius:6px; margin:0 0 8px;
      white-space:pre-wrap; word-break:break-all; font-size:.92rem; }
.meta { color:var(--muted); font-size:.85rem; margin-bottom:12px; }
/* Deny alone on the left, the two allow buttons on the right; never wraps,
   so a phone tap can't land on the wrong one after a reflow. */
.row { display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; }
.row .deny { margin-right:6px; }
button { font:inherit; font-size:.95rem; line-height:1.25; padding:10px 8px; min-height:44px;
         border-radius:6px; border:0; cursor:pointer; color:#fff; }
.once { background:var(--ok); } .session { background:var(--ok2); } .deny { background:var(--no); }
.ghost { background:transparent; color:var(--fg); border:1px solid var(--line); }
.empty { color:var(--muted); padding:8px 0; }
"""


def _shell_page(title: str, body: str, status: int = 200, script: str = "") -> HTMLResponse:
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main>{script}</body></html>"
    )
    return HTMLResponse(html, status_code=status)


def _not_found() -> HTMLResponse:
    return _shell_page(
        "Approval", "<h1>Not found</h1><p>The request expired or was answered, or the link is wrong.</p>", 404
    )


def _request_json(req: ApprovalRequest) -> dict:
    command, shell, cwd = req.key
    return {
        "id": req.id,
        "command": command,
        "shell": shell,
        "cwd": cwd,
        "expires_in": int(req.remaining()),
    }


_DASHBOARD_JS = """
<script>
const key = new URLSearchParams(location.search).get("key");
const esc = s => s.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function post(path, body) {
  await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
                     body: JSON.stringify(Object.assign({key}, body))});
  refresh();
}
function render(state) {
  const p = document.getElementById("pending"), a = document.getElementById("approved");
  document.title = (state.pending.length ? `(${state.pending.length}) ` : "") + "Approvals";
  p.innerHTML = state.pending.length ? state.pending.map(r => `
    <div class="card"><pre>${esc(r.command)}</pre>
      <div class="meta">in ${esc(r.cwd)} · ${esc(r.shell)} · expires in ${Math.floor(r.expires_in/60)}m ${r.expires_in%60}s</div>
      <div class="row">
        <button class="deny" onclick='post("/approvals/decide", {id: "${r.id}", decision: "deny"})'>Deny</button>
        <button class="once" onclick='post("/approvals/decide", {id: "${r.id}", decision: "once"})'>Allow once</button>
        <button class="session" onclick='post("/approvals/decide", {id: "${r.id}", decision: "session"})'>Allow until restart</button>
      </div></div>`).join("") : '<div class="empty">Nothing waiting.</div>';
  a.innerHTML = state.approved.length ? state.approved.map(r => `
    <div class="card"><pre>${esc(r.command)}</pre>
      <div class="meta">in ${esc(r.cwd)} · ${esc(r.shell)}</div>
      <button class="ghost" onclick='post("/approvals/revoke", {command: ${JSON.stringify(r.command)}, shell: ${JSON.stringify(r.shell)}, cwd: ${JSON.stringify(r.cwd)}})'>Revoke</button>
    </div>`).join("") : '<div class="empty">None.</div>';
}
async function refresh() {
  try {
    const r = await fetch("/approvals/state?key=" + encodeURIComponent(key));
    if (r.ok) render(await r.json());
  } catch (e) {}
}
refresh(); setInterval(refresh, 2000);
</script>
"""


def register_approval_routes(app) -> None:
    @app.custom_route("/approvals", methods=["GET"], include_in_schema=False)
    async def dashboard(request: Request) -> HTMLResponse:
        if not APPROVALS.dashboard_ok(request.query_params.get("key", "")):
            return _not_found()
        body = (
            "<h1>Command approvals</h1>"
            '<p class="muted">Commands the model wants to run on this machine that aren\'t in '
            "TRUSTED_COMMANDS. This page updates by itself; keep it open.</p>"
            '<h2>Waiting</h2><div id="pending"><div class="empty">Loading…</div></div>'
            '<h2>Allowed until restart</h2><div id="approved"></div>'
        )
        return _shell_page("Approvals", body, script=_DASHBOARD_JS)

    @app.custom_route("/approvals/state", methods=["GET"], include_in_schema=False)
    async def dashboard_state(request: Request) -> JSONResponse:
        if not APPROVALS.dashboard_ok(request.query_params.get("key", "")):
            return JSONResponse({"error": "not found"}, status_code=404)
        approved = [
            {"command": c, "shell": s, "cwd": d} for (c, s, d) in sorted(APPROVALS.approved)
        ]
        return JSONResponse(
            {"pending": [_request_json(r) for r in APPROVALS.pending()], "approved": approved}
        )

    async def _json(request: Request) -> dict:
        try:
            data = json.loads(await request.body() or b"{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    @app.custom_route("/approvals/decide", methods=["POST"], include_in_schema=False)
    async def dashboard_decide(request: Request) -> JSONResponse:
        data = await _json(request)
        if not APPROVALS.dashboard_ok(str(data.get("key", ""))):
            return JSONResponse({"error": "not found"}, status_code=404)
        req = APPROVALS.requests.get(str(data.get("id", "")))
        if req is None or not APPROVALS.decide(req, str(data.get("decision", "")), "dashboard"):
            return JSONResponse({"error": "not pending"}, status_code=409)
        return JSONResponse({"ok": True})

    @app.custom_route("/approvals/revoke", methods=["POST"], include_in_schema=False)
    async def dashboard_revoke(request: Request) -> JSONResponse:
        data = await _json(request)
        if not APPROVALS.dashboard_ok(str(data.get("key", ""))):
            return JSONResponse({"error": "not found"}, status_code=404)
        key = (str(data.get("command", "")), str(data.get("shell", "")), str(data.get("cwd", "")))
        return JSONResponse({"ok": APPROVALS.revoke(key)})

    # Per-request page: the link printed with each request.
    @app.custom_route("/approvals/{request_id}", methods=["GET"], include_in_schema=False)
    async def request_page(request: Request) -> HTMLResponse:
        req = APPROVALS.by_token(request.path_params["request_id"], request.query_params.get("token", ""))
        if req is None:
            return _not_found()
        command, shell, cwd = req.key
        if req.decided.is_set():
            return _shell_page("Approval", f"<h1>Already answered: {escape(_LABELS[req.decision])}</h1>"
                               f"<pre>{escape(command)}</pre>")
        buttons = "".join(
            f'<button class="{d}" name="decision" value="{d}">{_LABELS[d]}</button>'
            for d in (DENY, ONCE, SESSION)  # same order as the dashboard
        )
        body = (
            "<h1>Run this command?</h1>"
            '<p class="muted">The model wants to run a command that isn\'t in TRUSTED_COMMANDS, '
            "on this machine.</p>"
            f'<div class="card"><pre>{escape(command)}</pre>'
            f'<div class="meta">in {escape(cwd)} · {escape(shell)} · expires in about '
            f"{int(req.remaining())} s</div>"
            f'<form method="post" action="/approvals/{escape(req.id)}">'
            f'<input type="hidden" name="token" value="{escape(req.token)}">'
            f'<div class="row">{buttons}</div></form></div>'
        )
        return _shell_page("Approve command", body)

    @app.custom_route("/approvals/{request_id}", methods=["POST"], include_in_schema=False)
    async def request_decide(request: Request) -> HTMLResponse:
        form = await request.form()
        req = APPROVALS.by_token(request.path_params["request_id"], str(form.get("token", "")))
        if req is None:
            return _not_found()
        decision = str(form.get("decision", ""))
        if not APPROVALS.decide(req, decision, "page"):
            return _shell_page("Approval", "<h1>Already answered</h1>")
        return _shell_page("Approval", f"<h1>{escape(_LABELS[decision])}</h1><p>You can close this page.</p>")


__all__ = [
    "APPROVALS",
    "ApprovalDenied",
    "ApprovalPending",
    "authorize",
    "dashboard_url",
    "register_approval_routes",
    "resolve_request",
]
