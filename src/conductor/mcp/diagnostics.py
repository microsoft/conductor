"""Connection-scoped privacy for diagnostics emitted by the loaded MCP SDK."""

from __future__ import annotations

import logging
import ntpath
import os
from contextvars import ContextVar
from pathlib import Path, PureWindowsPath
from types import TracebackType
from typing import Any

SAFE_TRANSPORT_DIAGNOSTICS: ContextVar[bool] = ContextVar(
    "safe_mcp_transport_diagnostics", default=False
)
_installed = False


def _is_sdk_source(pathname: str, sdk_root: str) -> bool:
    """Compare normalized path components, resolving native symlinks before comparison."""
    if os.name == "nt":
        return Path(pathname).resolve().is_relative_to(Path(sdk_root).resolve())
    if ntpath.splitdrive(pathname)[0] or ntpath.splitdrive(sdk_root)[0]:
        # Foreign Windows paths cannot match a native POSIX package root.
        # PureWindowsPath also preserves component and case semantics in portable tests.
        return PureWindowsPath(pathname).is_relative_to(PureWindowsPath(sdk_root))
    return Path(pathname).resolve().is_relative_to(Path(sdk_root).resolve())


def install_safe_record_factory() -> None:
    """Delegate once per record and sanitize SDK-owned records before handlers."""
    global _installed
    if _installed:
        return
    import mcp

    assert mcp.__file__ is not None
    sdk_root = str(Path(mcp.__file__).resolve().parent)
    previous = logging.getLogRecordFactory()

    def safe_record(
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: Any,
        args: Any,
        exc_info: tuple[type[BaseException], BaseException, TracebackType | None] | None,
        func: str | None = None,
        sinfo: str | None = None,
        **kwargs: Any,
    ) -> logging.LogRecord:
        # Logging's callback contract requires these parameters and arbitrary message/args.
        # Classify the original caller, never a pathname rewritten by the previous factory.
        private = SAFE_TRANSPORT_DIAGNOSTICS.get() and (
            name == "mcp" or name.startswith("mcp.") or _is_sdk_source(fn, sdk_root)
        )
        record = previous(name, level, fn, lno, msg, args, exc_info, func, sinfo, **kwargs)
        if private:
            record.msg = "MCP transport diagnostic (payload suppressed)"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return record

    logging.setLogRecordFactory(safe_record)
    _installed = True
