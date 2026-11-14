"""Self-update: reinstall from GitHub, then restart running servers in place.

    unified-mcp-server --update            # latest main
    unified-mcp-server --update v0.3.0     # a branch, tag or commit
    unified-mcp-server --update --no-restart

Reinstalling uses the same tool that installed the package (``uv tool`` or
pip). Running HTTP servers of the current user are then sent SIGHUP; each
one waits a moment (so a reply in flight still goes out) and re-executes
itself with its original arguments: same PID, same terminal/tmux pane or
systemd unit, new code.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ARCHIVE = "https://github.com/waxz/unified-mcp-server/archive/{ref}.zip"
DIST = "unified-mcp-server"
RESTART_DELAY_S = 2.0


def _marker_dir() -> Path:
    """Where servers that can restart in place register their PID."""
    base = os.environ.get("XDG_RUNTIME_DIR") or str(Path.home() / ".cache")
    return Path(base) / "unified-mcp-server" / "restartable"


def _register_restartable() -> None:
    d = _marker_dir()
    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        (d / str(os.getpid())).write_text(" ".join(sys.argv))
    except OSError as exc:
        logger.info("Couldn't register for in-place restart: %s", exc)


def _is_restartable(pid: int) -> bool:
    return (_marker_dir() / str(pid)).is_file()


def installed_version() -> str:
    try:
        return metadata.version(DIST)
    except metadata.PackageNotFoundError:
        return "unknown"


# ------------------------------------------------------------------ install
@dataclass
class InstallMethod:
    kind: str  # "uv-tool", "pip", "editable"
    detail: str = ""


def detect_install(prefix: str | None = None, dist=None) -> InstallMethod:
    """How this copy was installed: uv tool, pip into an environment, or editable."""
    prefix = prefix or sys.prefix
    try:
        dist = dist or metadata.distribution(DIST)
        direct = dist.read_text("direct_url.json")
        if direct and json.loads(direct).get("dir_info", {}).get("editable"):
            return InstallMethod("editable", json.loads(direct).get("url", ""))
    except (metadata.PackageNotFoundError, ValueError):
        pass
    # Every `uv tool` environment has a receipt at its root (works with a
    # custom UV_TOOL_DIR too, unlike guessing from the path).
    if (Path(prefix) / "uv-receipt.toml").is_file():
        return InstallMethod("uv-tool", prefix)
    return InstallMethod("pip", prefix)


def update_command(method: InstallMethod, ref: str) -> list[str]:
    url = REPO_ARCHIVE.format(ref=ref)
    if method.kind == "uv-tool":
        uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
        python = f"{sys.version_info.major}.{sys.version_info.minor}"
        # --compile-bytecode: otherwise the restarted server compiles every
        # module on its first start, which makes the restart look stuck.
        return [uv, "tool", "install", "--reinstall", "--compile-bytecode", "--python", python,
                f"{DIST} @ {url}"]
    if method.kind == "pip":
        uv = shutil.which("uv")
        if uv:  # uv pip works even in environments without pip
            return [uv, "pip", "install", "--python", sys.executable, "--reinstall",
                    "--compile-bytecode", f"{DIST} @ {url}"]
        return [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", f"{DIST} @ {url}"]
    raise RuntimeError(
        f"This copy is an editable install ({method.detail}); update it with `git pull` "
        "in that checkout instead."
    )


# --------------------------------------------------------- running servers
def _cmdline(pid_dir: Path) -> list[str]:
    try:
        raw = (pid_dir / "cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def find_servers(proc: Path = Path("/proc"), uid: int | None = None) -> list[tuple[int, list[str]]]:
    """Running unified-mcp-server HTTP servers owned by ``uid`` (default: this user)."""
    uid = os.getuid() if uid is None else uid
    found = []
    for entry in proc.iterdir() if proc.is_dir() else []:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        args = _cmdline(entry)
        if not any(Path(a).name in ("unified-mcp-server", "unified_mcp_server") for a in args[:3]):
            if not ("-m" in args[:3] and "unified_mcp_server" in args[:4]):
                continue
        if any(a in ("--update", "--init-config", "--version", "-h", "--help") for a in args):
            continue
        if not _is_http(args):
            continue
        found.append((int(entry.name), args))
    return found


def _is_http(args: list[str]) -> bool:
    for i, a in enumerate(args):
        if a in ("-t", "--transport") and i + 1 < len(args):
            return args[i + 1] == "http"
        if a.startswith("--transport="):
            return a.split("=", 1)[1] == "http"
    # Transport may come from the config file; a stdio server is a child of
    # its client with its stdin a pipe, which we can't tell from here, so only
    # restart servers that say -t http explicitly.
    return False


def run_update(ref: str = "main", restart: bool = True) -> int:
    before = installed_version()
    method = detect_install()
    try:
        cmd = update_command(method, ref)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Updating {DIST} {before} ({method.kind}) from {REPO_ARCHIVE.format(ref=ref)}", file=sys.stderr)
    done = subprocess.run(cmd)
    if done.returncode != 0:
        print(f"error: update failed (exit {done.returncode}); nothing was restarted", file=sys.stderr)
        return done.returncode
    print("Installed.", file=sys.stderr)
    if not restart:
        print("Restart running servers to use it (kill -HUP <pid> restarts one in place).", file=sys.stderr)
        return 0
    if not hasattr(signal, "SIGHUP"):
        print("Restart the server yourself (no in-place restart on this platform).", file=sys.stderr)
        return 0
    servers = find_servers()
    if not servers:
        print("No running HTTP server found to restart.", file=sys.stderr)
    for pid, args in servers:
        if not _is_restartable(pid):
            # Started by an older version without the SIGHUP handler: SIGHUP
            # would terminate it rather than restart it.
            print(f"pid {pid} can't restart in place (started by an older version); restart it "
                  f"yourself: {' '.join(args[1:])}", file=sys.stderr)
            continue
        try:
            os.kill(pid, signal.SIGHUP)
            print(f"Restarting pid {pid} in place (back in a few seconds): {' '.join(args[1:])}",
                  file=sys.stderr)
        except OSError as exc:
            print(f"Couldn't signal pid {pid}: {exc}", file=sys.stderr)
    return 0


# ------------------------------------------------- in-place restart (server)
def _reexec_argv() -> tuple[str, list[str]]:
    argv0 = sys.argv[0]
    if argv0.endswith("__main__.py") or not os.access(argv0, os.X_OK):
        return sys.executable, [sys.executable, "-m", "unified_mcp_server", *sys.argv[1:]]
    # The console script's shebang points at the (possibly reinstalled) env.
    return argv0, list(sys.argv)


def _reexec() -> None:
    path, argv = _reexec_argv()
    logger.warning("Restarting in place: %s", " ".join(argv))
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os.execv(path, argv)


def install_restart_handler(delay: float = RESTART_DELAY_S) -> bool:
    """On SIGHUP, re-execute this server (same PID and arguments) after ``delay``."""
    if not hasattr(signal, "SIGHUP"):
        return False

    def on_hup(signum, frame):
        logger.warning("SIGHUP: restarting in %.0fs to load the installed code", delay)
        timer = threading.Timer(delay, _reexec)
        timer.daemon = True
        timer.start()

    signal.signal(signal.SIGHUP, on_hup)
    _register_restartable()
    import atexit

    atexit.register(lambda: (_marker_dir() / str(os.getpid())).unlink(missing_ok=True))
    return True
