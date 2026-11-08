"""Source-folder discovery.

Only the configured spreadsheet extensions are considered.  Temporary Office
lock files (``~$...``) and hidden files are ignored, and paths are never
followed outside the configured source root.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from .config import Settings
from .models import SourceFile
from .utils import ensure_dir, file_fingerprint, safe_relpath


class SourceNotFoundError(FileNotFoundError):
    """Raised when the configured source directory does not exist."""


def discover_files(settings: Settings, source_dir: Path | None = None) -> list[SourceFile]:
    root = (source_dir or settings.source_dir).expanduser().resolve()
    if not root.exists():
        raise SourceNotFoundError(f"数据源目录不存在: {root}")
    if not root.is_dir():
        raise SourceNotFoundError(f"数据源路径不是目录: {root}")
    ensure_dir(root)

    allowed = {extension.lower() for extension in settings.include_extensions}
    discovered: list[SourceFile] = []
    iterator = root.rglob("*") if settings.recursive_scan else root.glob("*")
    for path in iterator:
        if not path.is_file():
            continue
        name = path.name
        if name.startswith("~$") or name.startswith("."):
            continue
        suffix = path.suffix.lower()
        if suffix not in allowed:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size == 0:
            # Keep zero-byte files in the list: the run log should explicitly
            # record the empty-file compatibility path.
            pass
        discovered.append(
            SourceFile(
                path=path,
                relative_path=safe_relpath(path, root),
                suffix=suffix,
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime).astimezone(),
            )
        )

    discovered.sort(key=lambda item: (item.relative_path.lower(), item.modified_at))
    return discovered


def fingerprint_sources(files: list[SourceFile], root: Path) -> str:
    return file_fingerprint([source.path for source in files], root)


def count_files_by_suffix(files: list[SourceFile]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source in files:
        counts[source.suffix] = counts.get(source.suffix, 0) + 1
    return counts


def is_hidden(path: Path) -> bool:
    if path.name.startswith("."):
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes

        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & 0x2)
    except Exception:
        return False
