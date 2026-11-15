"""Sandboxing for untrusted shell commands.

A command that isn't in ``TRUSTED_COMMANDS`` is *untrusted*. There are three
ways this server can run one safely; which one is used is the **sandbox mode**:

* ``none``   -- don't run it blind: ask the user to approve it (see approvals).
* ``bubblewrap`` -- run it in a `bubblewrap <https://github.com/containers/bubblewrap>`_
  sandbox the server builds itself: the whole filesystem read-only except the
  project directory, a private ``/tmp``, and its own user/pid/ipc namespaces.
  Network is kept unless ``SANDBOX_NETWORK = "none"``. Linux only; needs the
  ``bubblewrap`` package. No Docker, no daemon, no external helper.
* ``docker`` -- run it in a throwaway ``docker run --rm`` container with only
  the project directory mounted, as the current user, with optional cpu/memory
  caps. Needs a working Docker daemon.
* ``drun``   -- the legacy path: hand off to a user-supplied ``drun`` shell
  function in ``~/.bashrc``. Kept for existing setups; the server can't verify
  what it does.

The mode is chosen with ``SANDBOX`` / ``--sandbox``. For backward
compatibility, if ``SANDBOX`` is unset but a sandbox work dir was configured
(``[DOCKER_CONFIG.<os>] work_dir``), the mode is ``drun``, as before.

Unlike the old ``drun`` path, the bubblewrap and docker argv pass the command
as a single ``argv`` element to ``bwrap``/``docker`` (never interpolated into
a shell string), so there is no outer-shell injection surface.
"""

from __future__ import annotations

import logging
import os
import shutil

from .. import config

logger = logging.getLogger(__name__)

NONE = "none"
BUBBLEWRAP = "bubblewrap"
DOCKER = "docker"
DRUN = "drun"
VALID_MODES = {NONE, BUBBLEWRAP, DOCKER, DRUN}

# What each mode needs on PATH (drun is a shell function, not a binary).
_MODE_BINARY = {BUBBLEWRAP: "bwrap", DOCKER: "docker"}


def effective_mode(settings: "config.Settings") -> str:
    """The active sandbox mode, resolving the unset default."""
    if settings.SANDBOX:
        return settings.SANDBOX
    # Back-compat: a configured sandbox work dir used to mean "wrap with drun".
    if settings.DOCKER_SANDBOX_WORKDIR:
        return DRUN
    return NONE


def is_active(settings: "config.Settings") -> bool:
    """True when untrusted commands are sandboxed (so they skip approval)."""
    return effective_mode(settings) != NONE


def validate(settings: "config.Settings") -> None:
    """Fail closed at startup: a chosen sandbox whose tool is missing, wrong
    platform, or lacks a writable root is an error, not a silent fallback."""
    mode = effective_mode(settings)
    if mode == NONE or settings.IS_IN_DOCKER:
        return
    if mode not in VALID_MODES:
        raise ValueError(f"SANDBOX must be one of {sorted(VALID_MODES)}, not {mode!r}")
    if mode == BUBBLEWRAP and settings.PLATFORM != "linux":
        raise ValueError("SANDBOX='bubblewrap' is Linux-only; use 'docker' or 'none' here")
    binary = _MODE_BINARY.get(mode)
    if binary and not shutil.which(binary):
        raise ValueError(
            f"SANDBOX={mode!r} needs '{binary}' on PATH, which isn't installed. "
            f"Install it, choose another SANDBOX mode, or use SANDBOX='none' (approval)."
        )
    if mode in (BUBBLEWRAP, DOCKER) and not settings.DOCKER_SANDBOX_HOST_ROOT:
        raise ValueError(
            f"SANDBOX={mode!r} needs a project directory to expose to the command; "
            "set -w / work_dir."
        )


def _network_allowed(settings: "config.Settings") -> bool:
    return str(settings.SANDBOX_NETWORK or "allow").lower() not in ("none", "off", "false")


# ----------------------------------------------------------------- builders
def build_bubblewrap_argv(
    shell_path: str,
    command: str,
    cwd: str,
    host_root: str,
    allow_network: bool = True,
) -> list[str]:
    """``bwrap`` argv: read-only root, writable project dir, private /tmp."""
    argv = [
        "bwrap",
        "--ro-bind", "/", "/",            # whole system, read-only
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--bind", host_root, host_root,   # project dir, writable (over the ro root)
        "--chdir", cwd,
        "--unshare-all",                  # user, ipc, pid, net, uts, cgroup
        "--new-session",                  # own session: no terminal-injection back to ours
        "--die-with-parent",
        "--setenv", "HOME", "/tmp",
    ]
    if allow_network:
        argv.append("--share-net")
    argv += ["--", shell_path, "-c", command]
    return argv


def build_docker_argv(
    shell_path: str,
    command: str,
    cwd: str,
    host_root: str,
    image: str,
    allow_network: bool = True,
    cpus: str | None = None,
    memory: str | None = None,
    uid: int | None = None,
    gid: int | None = None,
) -> list[str]:
    """``docker run --rm`` argv: only the project dir mounted, as this user."""
    uid = os.getuid() if uid is None and hasattr(os, "getuid") else uid
    gid = os.getgid() if gid is None and hasattr(os, "getgid") else gid
    argv = [
        "docker", "run", "--rm", "-i",
        "--network", "bridge" if allow_network else "none",
        "--security-opt", "no-new-privileges",
        "-v", f"{host_root}:{host_root}",
        "-w", cwd,
    ]
    if uid is not None and gid is not None:
        argv += ["--user", f"{uid}:{gid}"]
    if cpus:
        argv += ["--cpus", str(cpus)]
    if memory:
        argv += ["--memory", str(memory)]
    argv += [image, shell_path, "-c", command]
    return argv


def wrap_posix(shell_path: str, command: str, cwd: str, settings: "config.Settings") -> list[str] | None:
    """Sandbox argv for an untrusted POSIX command, or None when the caller
    should fall back to its own handling (``drun`` / ``none``)."""
    mode = effective_mode(settings)
    host_root = settings.DOCKER_SANDBOX_HOST_ROOT or cwd
    if mode == BUBBLEWRAP:
        return build_bubblewrap_argv(shell_path, command, cwd, host_root, _network_allowed(settings))
    if mode == DOCKER:
        return build_docker_argv(
            shell_path, command, cwd, host_root,
            image=settings.SANDBOX_DOCKER_IMAGE or "debian:stable-slim",
            allow_network=_network_allowed(settings),
            cpus=settings.DOCKER_SANDBOX_CPUS,
            memory=settings.DOCKER_SANDBOX_MEMORY,
        )
    return None  # drun / none handled by the adapter
