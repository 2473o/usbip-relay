"""Shared file logger for the USB/IP modules.

Each module writes to <server.logs>/<log_prefix><YYYY-MM-DD>.log. The file name
is resolved per write so a long-running relay rolls over at midnight without a
restart. Lines are serialized with a per-logger lock and writes never raise: a
full or read-only disk must not take a relay down.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

LEVELS = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARN": 3, "ERROR": 4, "FATAL": 5}


class Logger:
    def __init__(self, directory: str | Path, prefix: str, name: str, level: str = "DEBUG"):
        self.directory = Path(directory)
        self.prefix = prefix
        self.name = name
        level = level.upper()
        if level not in LEVELS:
            raise ValueError(f"unknown log level {level!r}, expected one of {', '.join(LEVELS)}")
        self.level = level
        self._lock = threading.Lock()

    def enabled(self, level: str) -> bool:
        return LEVELS[level.upper()] >= LEVELS[self.level]

    def path(self) -> Path:
        return self.directory / f"{self.prefix}{time.strftime('%Y-%m-%d')}.log"

    def log(self, level: str, msg: str) -> None:
        level = level.upper()
        if not self.enabled(level):
            return
        line = "%s | %-5s | %s | pid=%d | %s\n" % (
            time.strftime("%Y-%m-%dT%H:%M:%S%z"), level, self.name, os.getpid(), msg)
        with self._lock:
            try:
                with self.path().open("a") as f:
                    f.write(line)
            except OSError:
                pass

    def trace(self, msg: str) -> None:
        self.log("TRACE", msg)

    def debug(self, msg: str) -> None:
        self.log("DEBUG", msg)

    def info(self, msg: str) -> None:
        self.log("INFO", msg)

    def warn(self, msg: str) -> None:
        self.log("WARN", msg)

    def error(self, msg: str) -> None:
        self.log("ERROR", msg)

    def fatal(self, msg: str) -> None:
        self.log("FATAL", msg)
