# Derived from mcp-file-edit (MIT, https://github.com/waxz/mcp-file-edit);
# modified for unified-mcp-server. See NOTICE and LICENSE-MIT.
"""
Shared utilities and configuration for MCP file editor
"""

import os
import sys
import re
from pathlib import Path
from typing import Optional, Dict, Any
import mimetypes
import stat
from datetime import datetime

from .file_operations import FileOperationsInterface, LocalFileOperations
from .ssh_manager import SSHConnectionManager
from .git_operations import GitOperations, LocalGitOperations, SSHGitOperations
from .. import config


# =============================================================================
# Path Normalization Functions
# =============================================================================

def normalize_path(path: Path) -> str:
    """
    Convert a Path object to a normalized string that works across platforms.
    
    This function addresses a cross-platform compatibility issue:
    - On Windows, Path.absolute() returns paths like 'C:\\Users\\...'
    - When these paths are returned by the MCP server and used by clients
      running on different OSes (e.g., Linux), they fail to parse correctly
    
    Solution: Always use forward slashes and prefer relative paths when possible.
    
    Args:
        path: A Path object to convert to string
        
    Returns:
        A normalized path string using forward slashes (e.g., 'src/foo/bar')
    """
    # Convert to string and normalize slashes for cross-platform compatibility
    # This ensures paths work regardless of whether the server runs on
    # Windows, Linux, or macOS
    return str(path).replace(os.sep, '/')


def normalize_absolute_path(path: Path, base_dir: Optional[Path] = None) -> str:
    """
    Convert an absolute path to a normalized cross-platform string.
    
    On Windows, Path.absolute() returns 'C:\\Users\\name\\...'
    On Linux, it returns '/home/name/...'
    
    This function ensures consistent forward-slash output that works
    across all platforms when paths are exchanged between systems.
    
    Args:
        path: A Path object representing an absolute path
        base_dir: Optional base directory to make path relative to
        
    Returns:
        Normalized path string with forward slashes
    """
    if base_dir is not None:
        try:
            # Try to return a relative path first (preferred for cross-platform)
            return normalize_path(path.relative_to(base_dir))
        except ValueError:
            # Path is not relative to base_dir, fall through to absolute
            pass
    
    # Return absolute path with normalized slashes
    return normalize_path(path.absolute())


# =============================================================================
# File Type Classifications
# =============================================================================

# File type classifications
TEXT_EXTENSIONS = {
    '.txt', '.md', '.markdown', '.rst', '.log', '.csv', '.tsv',
    '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',
    '.xml', '.html', '.htm', '.xhtml', '.css', '.scss', '.sass',
    '.js', '.jsx', '.ts', '.tsx', '.vue', '.svelte',
    '.py', '.pyw', '.pyx', '.pyi', '.pyc',
    '.java', '.kt', '.scala', '.groovy',
    '.c', '.h', '.cpp', '.hpp', '.cc', '.cxx', '.c++',
    '.cs', '.fs', '.vb', '.swift', '.m', '.mm',
    '.go', '.rs', '.zig', '.nim', '.d',
    '.rb', '.php', '.pl', '.pm', '.lua',
    '.sh', '.bash', '.zsh', '.fish', '.ps1', '.bat', '.cmd',
    '.sql', '.r', '.R', '.jl', '.m', '.mat',
    '.tex', '.bib', '.cls', '.sty',
    '.Dockerfile', '.dockerignore', '.gitignore', '.env',
    '.scl', '.st',
    # ES modules / TypeScript variants (the host mime database often maps
    # these to application/*, which used to be misread as binary).
    '.mjs', '.cjs', '.mts', '.cts', '.jsonc', '.json5', '.jsonl', '.ndjson',
    '.astro', '.mdx',
    # Build / config / infra files commonly edited as text.
    '.lock', '.properties', '.editorconfig', '.gitattributes', '.gitmodules',
    '.npmrc', '.nvmrc', '.bazel', '.bzl', '.cmake', '.mk', '.make', '.makefile',
    '.gradle', '.tf', '.tfvars', '.hcl', '.proto', '.graphql', '.gql',
    '.patch', '.diff', '.po', '.pot', '.srt', '.vtt', '.adoc', '.org', '.rst',
    # More languages.
    '.ex', '.exs', '.erl', '.hrl', '.clj', '.cljs', '.cljc', '.edn',
    '.hs', '.elm', '.dart', '.sol', '.vim', '.el', '.lisp', '.scm',
    '.f', '.f90', '.f95', '.pas', '.asm', '.s', '.ron', '.v', '.sv',
}

BINARY_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.ico', '.webp', '.svg',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.zip', '.tar', '.gz', '.bz2', '.7z', '.rar',
    '.exe', '.dll', '.so', '.dylib', '.a', '.o',
    '.mp3', '.mp4', '.avi', '.mov', '.wmv', '.flv',
    '.ttf', '.otf', '.woff', '.woff2', '.eot'
}

# Global base directory (startup cwd; retained for compatibility in outputs/tests)
BASE_DIR: Optional[Path] = None

# Global project directory: single authoritative local root for path safety/routing
PROJECT_DIR: Optional[Path] = None

# Resolved paths the file tools must never touch (the config file holding API
# keys, plus PROTECTED_PATHS). Checked by is_safe_path, which every file tool
# and directory walker goes through.
PROTECTED_PATHS: list[Path] = []

# Global file operations backend and SSH manager
FILE_OPS: FileOperationsInterface = LocalFileOperations()
SSH_MANAGER = SSHConnectionManager()
CONNECTION_TYPE = "local"  # "local" or "ssh"
GIT_OPS: Optional[GitOperations] = None  # Initialized when needed

UNSAFE_PATH_CHARS = {"|", "&", ";", "`", "$", ">", "<", "\x00", "\n", "\r"}


def validate_path_input(path: str) -> None:
    """Reject path strings that contain shell/control metacharacters."""
    if any(ch in path for ch in UNSAFE_PATH_CHARS):
        raise ValueError("Invalid path: unsafe characters detected")

    normalized = path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if ".." in parts:
        raise ValueError("Invalid path: directory traversal detected")

    # Cross-platform style guards:
    # - Linux/WSL/macOS: reject Windows-style paths (backslashes, drive letter, UNC)
    # - Windows: reject POSIX-style absolute paths (/...)
    if os.name != "nt":
        if "\\" in path or re.match(r"^[A-Za-z]:[\\/]", path) or path.startswith("\\\\"):
            raise ValueError("Invalid path: Windows-style paths are not allowed on this platform")
    else:
        if path.startswith("/"):
            raise ValueError("Invalid path: POSIX absolute paths are not allowed on this platform")


def get_allow_directories() -> list[Path]:
    """
    Get allowed local root directories for set_project_directory.

    Reads runtime config loaded from config.toml / CLI.
    When unset, config normalizes this to the active WORK_DIR so the initial
    PROJECT_DIR is always inside the allowed roots.
    """
    allow_directories = config.SETTINGS.ALLOWED_DIRECTORIES

    # Preserve order, remove duplicates
    unique: list[Path] = []
    seen: set[str] = set()
    for root in allow_directories:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(Path(root))
    return unique


def is_within_allowed_directories(path: Path, allowed_roots: list[Path]) -> bool:
    """Return True if path is inside at least one allowed root."""
    try:
        resolved = path.resolve()
        for root in allowed_roots:
            try:
                if resolved.is_relative_to(root.resolve()):
                    return True
            except (ValueError, RuntimeError):
                continue
        return False
    except (ValueError, RuntimeError):
        return False


def is_safe_path(path: Path) -> bool:
    """
    Check if a path is safe to access (no directory traversal).
    
    Security logic:
    - For SSH connections: paths are remote, skip local boundary checks
    - For local connections: enforce path within PROJECT_DIR
    """
    # For SSH connections, paths are on remote server - skip local path check
    if CONNECTION_TYPE == "ssh":
        return True
    
    try:
        if PROJECT_DIR is None:
            return False
        resolved = path.resolve()
        project_resolved = PROJECT_DIR.resolve()
        if not resolved.is_relative_to(project_resolved):
            return False
        return not is_protected(resolved)
    except (ValueError, RuntimeError):
        return False


def is_protected(path: Path) -> bool:
    """True for a protected file, or anything inside a protected folder."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return True
    return any(resolved == p or resolved.is_relative_to(p) for p in PROTECTED_PATHS)


def resolve_path(path: str, work_dir: str | None = None) -> Path:
    """
    Resolve a path relative to PROJECT_DIR for local/SSH operations.
    
    Args:
        path: Input path (can be relative or absolute)
        
    Returns:
        Resolved Path object
        
    Security:
        - For SSH: absolute paths are allowed (remote paths)
        - For local: validation is handled by is_safe_path against PROJECT_DIR
    """
    validate_path_input(path)
    path_obj = Path(path)
    if path_obj.is_absolute():
        return path_obj

    if work_dir:
        return Path(work_dir)/ path
    if PROJECT_DIR:
        return PROJECT_DIR / path

    return BASE_DIR / path


# application/* mime types (and structured suffixes) that are actually text.
# The default fallback treats every application/* type as binary, which is
# wrong for these -- and which type the host reports for e.g. `.mjs` depends
# on its /etc/mime.types, so it varies by machine.
_TEXTUAL_APPLICATION_TYPES = {
    "application/javascript", "application/x-javascript", "application/ecmascript",
    "application/json", "application/manifest+json", "application/xml",
    "application/xhtml+xml", "application/toml", "application/x-toml",
    "application/yaml", "application/x-yaml", "application/x-sh",
    "application/x-shellscript", "application/graphql", "application/sql",
    "application/x-tex", "application/x-latex", "application/x-perl",
    "application/x-python", "application/x-ruby", "application/x-httpd-php",
    "application/x-yaml", "application/csv",
}
_TEXT_MIME_SUFFIXES = ("+json", "+xml", "+yaml", "+toml")


def _mime_is_text(mime_type: str) -> bool:
    mime_type = mime_type.lower()
    if mime_type.startswith("text/"):
        return True
    if mime_type in _TEXTUAL_APPLICATION_TYPES:
        return True
    return any(mime_type.endswith(suffix) for suffix in _TEXT_MIME_SUFFIXES)


def sniff_is_text(path: Path, sample_size: int = 8192) -> Optional[bool]:
    """Decide text vs binary from the file's own bytes (host-independent).

    Returns True (text), False (binary), or None when the bytes can't be read
    (a directory, a permission error, or a remote path).
    """
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(sample_size)
    except OSError:
        return None
    if not chunk:
        return True  # an empty file is editable as text
    if b"\x00" in chunk:
        return False  # a NUL byte means binary
    try:
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        # A multibyte character may have been split at the sample boundary.
        if exc.start >= len(chunk) - 4:
            try:
                chunk[: exc.start].decode("utf-8")
                return True
            except UnicodeDecodeError:
                pass
    # Not UTF-8: accept legacy text (latin-1 etc.) by its printable ratio.
    printable = bytes(range(0x20, 0x7F)) + b"\t\n\r\f\v\b\x1b"
    non_text = sum(byte not in printable for byte in chunk)
    return (non_text / len(chunk)) < 0.30


def get_file_type(path: Path) -> str:
    """Determine whether a file should be handled as text or binary."""
    suffix = path.suffix.lower()
    if suffix in TEXT_EXTENSIONS or path.name in TEXT_EXTENSIONS:
        return "text"
    if suffix in BINARY_EXTENSIONS:
        return "binary"
    # For a local file, the bytes are authoritative and don't depend on the
    # host's mime database (which is what made `.mjs` read as binary).
    if CONNECTION_TYPE == "local":
        sniffed = sniff_is_text(path)
        if sniffed is not None:
            return "text" if sniffed else "binary"
    # Remote path, or bytes unreadable: fall back to mimetypes, but don't treat
    # textual application/* types as binary.
    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type:
        if _mime_is_text(mime_type):
            return "text"
        if mime_type.startswith(("image/", "audio/", "video/", "font/", "application/")):
            return "binary"
    return "unknown"


async def get_file_info_async(path: Path) -> Dict[str, Any]:
    """
    Get detailed file information using the current file operations backend.
    
    Returns normalized cross-platform paths to ensure compatibility when
    the MCP server is used by clients on different operating systems.
    """
    try:
        if CONNECTION_TYPE == "local" and not is_safe_path(path):
            raise ValueError("Invalid path: directory traversal detected")

        stat_info = await FILE_OPS.stat(path)
        file_type = get_file_type(path)
        
        info = {
            "name": path.name,
            "path": str(path),
            "type": "directory" if await FILE_OPS.is_dir(path) else "file",
            "size": stat_info.st_size,
            "modified": datetime.fromtimestamp(stat_info.st_mtime).isoformat(),
            "created": datetime.fromtimestamp(stat_info.st_ctime).isoformat(),
            "permissions": stat.filemode(stat_info.st_mode),
            "file_type": file_type
        }
        
        # Cross-platform: use normalized paths instead of platform-specific paths
        # This fixes the issue where Windows paths (C:\...) cause errors on Linux
        if CONNECTION_TYPE == "local":
            info["absolute_path"] = normalize_absolute_path(path, PROJECT_DIR)
            try:
                info["relative_path"] = normalize_path(path.relative_to(PROJECT_DIR))
            except ValueError:
                info["relative_path"] = normalize_path(path)
        
        # Add line count for text files
        if file_type == "text" and not await FILE_OPS.is_dir(path):
            try:
                content = await FILE_OPS.read_file(path)
                info["line_count"] = len(content.splitlines())
            except:
                info["line_count"] = None
        
        return info
    except Exception as e:
        return {
            "name": path.name,
            "path": str(path),
            "type": "unknown",
            "error": str(e)
        }


def get_file_info_sync(path: Path) -> Dict[str, Any]:
    """
    Get detailed file information.
    
    Returns normalized cross-platform paths to ensure compatibility when
    the MCP server is used by clients on different operating systems.
    """
    try:
        stat_info = path.stat()
        file_type = get_file_type(path)
        
        # Cross-platform: use normalized paths instead of platform-specific paths
        info = {
            "name": path.name,
            # Return relative path when possible (works best across platforms)
            "path": normalize_absolute_path(path, PROJECT_DIR),
            # Also provide normalized absolute path
            "absolute_path": normalize_absolute_path(path),
            "type": "directory" if path.is_dir() else "file",
            "size": stat_info.st_size,
            "modified": datetime.fromtimestamp(stat_info.st_mtime).isoformat(),
            "created": datetime.fromtimestamp(stat_info.st_ctime).isoformat(),
            "permissions": stat.filemode(stat_info.st_mode),
            "file_type": file_type
        }
        
        # Add line count for text files
        if file_type == "text" and path.is_file():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    info["line_count"] = sum(1 for _ in f)
            except:
                info["line_count"] = None
                
        return info
    except Exception as e:
        return {
            "name": path.name,
            "error": str(e)
        }


def get_git_operations() -> Optional[GitOperations]:
    """Get or initialize git operations based on current connection type."""
    global GIT_OPS
    
    if GIT_OPS is None and PROJECT_DIR is not None:
        if CONNECTION_TYPE == "ssh":
            git_backend = SSHGitOperations(SSH_MANAGER.connection, SSH_MANAGER.sftp)
        else:
            git_backend = LocalGitOperations()
        
        GIT_OPS = GitOperations(git_backend, FILE_OPS, PROJECT_DIR)
    
    return GIT_OPS
