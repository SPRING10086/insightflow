"""Application logging and machine-readable run history."""

from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .config import Settings

_LOGGER_NAME = "insightflow"
_CONFIGURED_FOR: str | None = None


def configure_logging(settings: Settings) -> logging.Logger:
    """Configure a rotating text log plus a concise console handler."""
    global _CONFIGURED_FOR
    logger = logging.getLogger(_LOGGER_NAME)
    log_dir = settings.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    key = str(log_dir.resolve())
    if _CONFIGURED_FOR == key:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_dir / "insightflow.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(logging.Formatter("%(message)s"))
    console.setLevel(logging.INFO)
    logger.addHandler(console)
    _CONFIGURED_FOR = key
    return logger


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()


def read_run_history(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return records[-limit:][::-1]
