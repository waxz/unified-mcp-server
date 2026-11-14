# Using unified-mcp-server well

Practices from running it day to day as a remote MCP connector (for example
claude.ai, through a Cloudflare tunnel) on a development machine.

## 1. Set it up

```bash
mkdir -p ~/.config/unified-mcp-server
unified-mcp-server --init-config ~/.config/unified-mcp-server/config.toml
```

- **Keep the config outside the project folder.** It holds `API_KEYS`. The
  file tools refuse the config file they loaded (and anything in
  `PROTECTED_PATHS`), but shell commands you approve can still read anything.
- **Point `-w` at the folder you work in**, e.g. `~/dev`, not your home
  directory. The file tools can read and write everything under it.
- **Always set an API key over HTTP**, and give the same key to the client
  (connector setting, `Authorization: Bearer …` or `x-api-key`).
- Listen on `127.0.0.1` and publish it through a tunnel; don't bind
  `0.0.0.0` on an untrusted network.

```bash
unified-mcp-server -c ~/.config/unified-mcp-server/config.toml -w ~/dev -t http -H 127.0.0.1 -P 7000 -p /mcp
```

## 2. Run shell commands

Three ways, in order of preference:

| Use | How | Prompt? |
|---|---|---|
| Things you run often (tests, builds, `git` helpers) | `[TRUSTED_COMMANDS.name]` in the config; the model calls it by name | no |
| One-off commands | any command line; you approve it | yes, once or until restart |
| Isolation | configure the `drun` sandbox (`[DOCKER_CONFIG.<os>] work_dir`) | no |

Trusted commands pin the command line and working directory, so promote a
command to `TRUSTED_COMMANDS` once you find yourself approving it repeatedly.

**Timeouts.** `execute_command` stops a command after `COMMAND_TIMEOUT` (30 s)
unless the call passes `timeout` (up to `COMMAND_TIMEOUT_MAX`, 600 s). Builds
fit in that. For anything longer, or for servers that keep running (`pnpm run
dev`, `wrangler dev`, a tunnel), use `tmux_execute`; it has no time limit.

**Long output.** `tmux_get_output` returns the visible screen; pass
`lines=200` (up to 5000) for the last N lines including scrollback, e.g. a
build log or a URL printed at startup. New sessions are 200×50.

Clean up: `tmux_kill_session native_mcp_<name>` when a job is done.

## 3. Approve commands

Open the **approvals dashboard** once and keep the tab open: its link is
printed in the server's log at startup and changes on every restart. The tab
title shows how many requests are waiting.

- **Allow once** for anything unusual; **Allow until restart** for something
  the model will repeat in this session; **Deny** stops it for good.
- A call waits up to `APPROVAL_WAIT` (25 s). If you're slower, it returns
  "approval pending" and nothing runs; the model calls again with the same
  arguments and it runs as soon as you approve. Requests stay open for
  `APPROVAL_TIMEOUT` (10 min).
- In claude.ai the request also appears as a prompt: *Submit* allows once
  (tick the box for until restart), *Decline* denies. The app stacks those
  two buttons; the dashboard keeps Deny apart from the allow buttons.
- Read the command and the folder before approving. Approval covers the exact
  command line, so `cmd && something-else` is approved as a whole.

## 4. Keep secrets away from the model

- Links with tokens (dashboard, per-request pages) appear only in the server's
  log. Keep that log out of folders the file tools can read; never paste the
  dashboard link into a chat.
- Put other secrets (`.env`, key files) in `PROTECTED_PATHS`, or outside `-w`.
- `ALLOW_HOST_TMUX_INPUT` stays `false`: typing into a host tmux session
  would run arbitrary commands without approval.
- Exposing another local service (e.g. a dev server without authentication)?
  Put an authenticating gateway in front of it and tunnel the gateway, never
  the service itself. Keep the gateway's key outside `-w` too.

## 5. Upgrade and restart

Restarting the server from one of its own tool calls kills the call that does
it, so schedule the restart as a detached job and let the call return first.

**Server running in a tmux pane** (e.g. pane `2:1.1`):

```bash
uv tool install --reinstall --python 3.12 "unified-mcp-server @ https://github.com/waxz/unified-mcp-server/archive/refs/heads/main.zip"
setsid nohup bash -c 'sleep 5; tmux send-keys -t 2:1.1 C-c; sleep 4; tmux send-keys -t 2:1.1 "unified-mcp-server -c ~/.config/unified-mcp-server/config.toml -w ~/dev -t http -H 127.0.0.1 -P 7000 -p /mcp" Enter' >/dev/null 2>&1 < /dev/null &
```

**As a systemd user service** (not yet tried in practice). Save as
`~/.config/systemd/user/unified-mcp-server.service`:

```ini
[Unit]
Description=unified-mcp-server

[Service]
WorkingDirectory=%h/dev
ExecStart=%h/.local/bin/unified-mcp-server -c %h/.config/unified-mcp-server/config.toml -w %h/dev -t http -H 127.0.0.1 -P 7000 -p /mcp
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now unified-mcp-server
journalctl --user -u unified-mcp-server | grep "Approvals dashboard"
```

Restart it from a tool call with a delay, so the call can return first:
`systemd-run --user --on-active=5 systemctl --user restart unified-mcp-server`.

After a restart: approvals given "until restart" are gone, the dashboard link
is new, and tmux sessions started by the server keep running.

## 6. Troubleshooting

| Message | Meaning / fix |
|---|---|
| `Unauthorized: missing or invalid API key` | The client isn't sending the key from `API_KEYS`. |
| `Waiting for the user to approve …` | Approve it on the dashboard or in the prompt; the model retries. |
| `The user denied …` / `… expired` | Denied or unanswered for 10 minutes; ask before retrying. |
| `Directory not allowed` | `cwd` is outside the allowed roots (`-w`, `-d`). |
| `Timed out after 30s …` | Pass `timeout` (≤ 600) or use `tmux_execute`. |
| `tmux session '0' is not a native_mcp_ session …` | Only sessions the server created are reachable; your own tmux sessions aren't. |
| `Typing into host tmux session … is disabled` | By design; see `ALLOW_HOST_TMUX_INPUT`. |
| `Invalid path: …` / file tool refuses a file | Path outside `-w`, traversal, or a protected file. |
| Tool descriptions look old after an upgrade | The client caches the tool list; reconnect it. |
