# unified-mcp-server

A self-contained MCP server with file editing and shell execution in one
process, one endpoint and one config file. It has 58 tools:

| Family | Tools |
|---|---|
| Files | read/write/create/copy/move/delete, search & replace, `patch_file`, `apply_patch` with preview → confirm → apply (plus a browser review page over HTTP), `get_file_info` |
| Git | status, init, clone, add, commit, push, pull, log, branch, checkout, diff, remote |
| Code analysis | tree-sitter `list_functions`, `get_function_at_line`, `get_code_structure`, `search_functions` |
| Linting | `detect_linters`, `run_linter`, `lint_file`, `run_type_checker`, `type_check_file`, `format_file` |
| Remote | `ssh_upload`, `ssh_download`, `ssh_sync`, SSH project directories |
| Project | `set_project_directory`, `get_project_directory` |
| Shell | `execute_command` (streamed output), `list_trusted_commands`, process list/terminate, tmux sessions |

The code started as two separate servers,
[mcp-file-edit](https://github.com/waxz/mcp-file-edit) and
[shell-mcp-server](https://github.com/waxz/shell-mcp-server). It is copied
into this package and maintained here; neither project is a dependency.

## Install

One line installs [uv](https://docs.astral.sh/uv/) (which fetches Python 3.12
if needed) and then the `unified-mcp-server` command. Git is not required.

**Ubuntu / Linux / macOS**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh && ~/.local/bin/uv tool install --python 3.12 "unified-mcp-server @ https://github.com/waxz/unified-mcp-server/archive/refs/heads/main.zip"
```

**Windows (PowerShell)**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"; & "$env:USERPROFILE\.local\bin\uv.exe" tool install --python 3.12 "unified-mcp-server @ https://github.com/waxz/unified-mcp-server/archive/refs/heads/main.zip"
```

The command is installed in `~/.local/bin` (`%USERPROFILE%\.local\bin` on
Windows). If `unified-mcp-server` isn't found afterwards, open a new terminal or
run `uv tool update-shell`.

Update with `uv tool install --reinstall` and the same URL; remove with
`uv tool uninstall unified-mcp-server`.

On Windows the shell tools run commands through PowerShell (`bash` maps to
`powershell.exe`) and the tmux tools are unavailable. CI runs on Ubuntu only.

From a checkout, for development:

```bash
uv venv && uv pip install -e ".[dev]"
```

## Run

stdio (Claude Desktop, Claude Code):

```bash
unified-mcp-server -w /path/to/project
```

```bash
claude mcp add unified -- unified-mcp-server -w /path/to/project
```

HTTP:

```bash
unified-mcp-server -c config.toml -t http -H 127.0.0.1 -P 8000 -p /mcp
```

### CLI

| Flag | Meaning |
|---|---|
| `-c/--config` | TOML config. Optional (see below) |
| `--init-config [PATH]` | write a commented config template to PATH (default `config.toml`, `-` prints it) and exit; `--force` overwrites |
| `-t/--transport` | `stdio` or `http` |
| `-H/--host`, `-P/--port`, `-p/--path` | HTTP bind settings |
| `-w/--workdir` | project directory |
| `-d/--directories` | allowed root directories |
| `--shell NAME PATH` | shell mapping for shell tools (repeatable) |
| `--namespaced` | prefix tools with `fs_` / `shell_` |
| `--no-files` / `--no-shell` | disable one family |
| `--api-key KEY` | API key for HTTP clients (repeatable; replaces `API_KEYS`) |
| `--command-timeout SECONDS` | shell command timeout (`COMMAND_TIMEOUT`) |
| `--safety-mode strict\|relax` | shell safety mode (`SAFETY_MODE`) |
| `--public-url URL` | base URL for patch-preview links (`PUBLIC_URL`) |
| `--in-docker` | server runs in a container; run commands directly (`IS_IN_DOCKER`) |
| `--no-approval` | refuse untrusted commands instead of asking you (`APPROVE_UNTRUSTED = false`) |
| `--approval-timeout SECONDS` | how long an approval request stays open (`APPROVAL_TIMEOUT`, default 600) |
| `--approval-wait SECONDS` | how long one tool call waits before returning "pending" (`APPROVAL_WAIT`, default 25) |
| `--allow-host-tmux-input` | let `tmux_send_keys` type into host sessions (`ALLOW_HOST_TMUX_INPUT`; runs arbitrary commands) |

Precedence: installed default config → your config file → CLI flags.
Environment variables are not read.

A config file is optional. Which one is used:

- `-c FILE` that exists: that file.
- `-c FILE` that doesn't exist: a warning names the missing file, and the
  installed default config is used.
- no `-c`: `./config.toml` if it exists, otherwise the installed default config.

So a quick HTTP server with a key needs no file at all:

```bash
unified-mcp-server -t http -H 127.0.0.1 -P 8000 -p /mcp --api-key change-me
```

Arguments are visible to other users on the same machine (for example in
`ps`), so on shared machines put `API_KEYS` in a config file instead of using
`--api-key`. Trusted shell commands can only be defined in a config file.

## Configuration

Generate a starting config with every option documented:

```bash
unified-mcp-server --init-config            # writes ./config.toml
unified-mcp-server -c config.toml -t http
```

The template fills in a freshly generated API key, so the file is created
readable by you only (mode 600). It won't overwrite an existing file unless
you pass `--force`. `--init-config -` prints the template instead.

Config files written for either original project use the same format and load
as they are, as long as the directories they name exist on this machine. Keys
this server doesn't know are logged and ignored.

- **Project directory.** `[CONFIG.<os>] work_dir` / `-w` sets the root for
  file tools. Unless `[DOCKER_CONFIG.<os>] host_root` is set, shell commands
  with `cwd="."` run there too, and follow `set_project_directory`. Allowed
  roots are shared the same way.
- **Auth.** `API_KEYS = "k1,k2"` protects every tool call over HTTP. Send
  `Authorization: Bearer <key>` or `x-api-key: <key>`. Keys are compared in
  constant time and never echoed back. stdio has no headers, so keys are
  ignored there.

### Shell execution model

- `TRUSTED_COMMANDS` run directly on the host, by name:
  `execute_command(command="t_test", cwd=".")`.
- Any other command, when no sandbox is configured, needs **your approval**
  (`APPROVE_UNTRUSTED = true`, the default). Answer wherever is handiest:
  - **Approvals dashboard** (HTTP): the server prints its link once at
    startup. Keep the tab open: it lists every waiting request, updates by
    itself, shows the count in the tab title, and lets you revoke commands
    you allowed earlier. The link changes on every restart.
  - **Your MCP client's prompt**, if it supports MCP elicitation: *Submit*
    allows the command once (tick the checkbox to allow it until restart),
    *Decline* denies it. The client draws those buttons; the server only adds
    the checkbox.
  - **A per-request link** printed in the server log with each request.

  Answers: **Allow once**, **Allow until restart** (the same command, shell
  and working directory then run without asking until the server restarts),
  or **Deny**. Nothing is written to `config.toml`.

  A request stays open for `APPROVAL_TIMEOUT` seconds (default 600). A tool
  call waits up to `APPROVAL_WAIT` seconds (default 25) for your answer; if
  you haven't answered yet it returns "approval pending" without running
  anything, and the model calls again with the same arguments once you have.
  While your client is showing its own prompt, the call waits for that answer
  instead.

  Links carry random tokens and appear only in the server's log, never in what
  the model sees, so the model can't approve its own requests. Keep that log
  outside the folders the file tools can read. The working directory must
  still be inside the allowed roots. `--no-approval` refuses untrusted
  commands instead. Over stdio with a client that can't show prompts there is
  no way to approve, so the command is refused; use `TRUSTED_COMMANDS` there.
- With a sandbox configured (`[DOCKER_CONFIG.<os>] work_dir`), other
  commands run in it through the external `drun` helper (expected in
  `~/.bashrc`) instead of asking for approval.
- Set `IS_IN_DOCKER = true` when the server itself runs inside a container:
  every command then runs directly in it.
- `SAFETY_MODE = "strict"` forces `bash` for untrusted commands.
- tmux: `tmux_execute` creates host sessions named `native_mcp_<name>`, and
  the other tmux tools act on those directly. Any other session name is
  treated as a sandbox session, so without a sandbox configured those calls
  return an error. Your own tmux sessions aren't reachable.
- `tmux_send_keys` can't type into host (`native_mcp_*`) sessions unless
  `ALLOW_HOST_TMUX_INPUT = true` (or `--allow-host-tmux-input`). Typing into a
  host shell runs any command, which would bypass `TRUSTED_COMMANDS`, so it is
  off by default.

## Changes from the original projects

**Structure**

- `files/` holds the file tools and `shell/` the shell tools. Code both
  projects duplicated lives once in `common/`.
- Both tool families share one `Settings` model, one loader and one FastMCP
  app. Tools are registered directly; there is no proxying or mounting.
- Settings is a plain pydantic model, not `BaseSettings`, so the system
  `PATH`/`HOST` environment variables can't leak into the server's
  `PATH`/`HOST` fields.
- The unused `fuzzy_patch.py` (it needed an uninstalled library) and a
  debugging tmux helper that printed to stdout were not copied.

**Bugs fixed**

- `tmux_execute` sent `C-c` to a new pane before its shell had started,
  killing the shell and the tmux server. This failed every time on GitHub
  runners. The bootstrap now waits for the pane's prompt and the reset no
  longer sends `C-c`. *(shell/tmux_commands.py)*
- `tmux_get_output`, `tmux_send_keys` and `tmux_kill_session` crashed
  (`cwd=None`) or failed with "Directory not allowed" on any session not
  named `native_mcp_*` when no sandbox was configured. They now return an
  error that explains why. *(shell/tool_handlers.py)*
- `tmux_list_session` crashed with `cwd=None` when no sandbox work dir was
  configured, and raised when no tmux server was running. It now checks the
  sandbox only when one exists, returns `[]` with no server, and drops blank
  names. *(shell/tool_handlers.py)*
- `remove_directory` and recursive `delete_file` used
  `shutil.rmtree(onexc=...)`, which needs Python 3.12. They now work on 3.11.
  *(files/file_operations.py)*
- Code analysis skipped every `async def` (no `visit_AsyncFunctionDef`).
  *(files/code_analyzer.py)*
- A timed-out command crashed while formatting its result (`config` was never
  imported). *(common/mcp_types_utils.py)*
- Auth: Bearer tokens never worked, because the `Authorization` header was
  stripped. A failed check also echoed the key and all request headers back to
  the caller. *(server.py)*
- fastmcp 4.x removed `fastmcp.server.tasks`; the dependency is pinned `<4`.

## Tests & CI

```bash
uv pip install -e ".[dev]"
pytest -q
```

- `tests/test_tools.py` calls every tool family through a real MCP client. It
  also checks the exact set of 58 registered tools.
- `tests/test_unified.py` covers config, namespacing, the shared project
  directory, the installed console script over stdio, and API-key auth over
  real HTTP.
- `tests/test_fixes.py` holds a regression test for each fixed bug, including
  a reproduction of the tmux race using a shell that is slow to start.

CI (`.github/workflows/ci.yml`) runs the suite on Ubuntu with
Python 3.11, 3.12 and 3.13.

## License

The code comes from an MIT-licensed project (mcp-file-edit) and an
Apache-2.0-licensed project (shell-mcp-server), so the package is
`MIT AND Apache-2.0`. See `NOTICE`, `LICENSE-MIT` and `LICENSE-APACHE`. Each
copied file says which project it came from.
