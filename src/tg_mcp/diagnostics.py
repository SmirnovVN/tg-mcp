import hashlib
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from secrets import token_hex
from time import monotonic


logger = logging.getLogger("tg_mcp.diagnostics")
logger.setLevel(logging.WARNING)


@dataclass
class _Call:
    id: str
    rpc_count: int = 0


_current: ContextVar[_Call | None] = ContextVar("telegram_tool_call", default=None)


@contextmanager
def trace_tool(name: str):
    if not logger.isEnabledFor(logging.INFO):
        yield
        return
    call = _Call(token_hex(8))
    token = _current.set(call)
    start = monotonic()
    try:
        yield
    finally:
        logger.info("call=%s tool=%s duration_ms=%.1f rpc_count=%d",
                    call.id, name, (monotonic() - start) * 1000, call.rpc_count)
        _current.reset(token)


def log_rpc(method: str, start: float, status: str, wait: int = 0) -> None:
    call = _current.get()
    if call is not None:
        if status != "cooldown":
            call.rpc_count += 1
        logger.info("call=%s method=%s duration_ms=%.1f status=%s retry_after_seconds=%d",
                    call.id, method, (monotonic() - start) * 1000, status, wait)


def log_tool_error(name: str, error: BaseException) -> None:
    call = _current.get()
    logger.error(
        "call=%s tool=%s error_type=%s",
        call.id if call is not None else "-", name, type(error).__name__,
    )


def log_cursor(action: str, cursor: str, kind: str | None = None) -> None:
    call = _current.get()
    if call is not None:
        fingerprint = hashlib.sha256(cursor.encode("ascii")).hexdigest()[:16]
        logger.info("call=%s cursor_%s=%s%s", call.id, action, fingerprint, f" kind={kind}" if kind else "")
