"""Retry / backoff helper for LLM provider calls.

All providers share the same transient failure profile: network blips,
429 rate-limit, 5xx upstream errors. Wrap their SDK awaitables with
:func:`call_with_retry` so a single burst doesn't tank a long generation run.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 1.0  # seconds
DEFAULT_MAX_DELAY = 10.0


def _is_retryable(exc: BaseException) -> bool:
    """Best-effort classification across SDKs."""
    name = type(exc).__name__.lower()
    if any(tok in name for tok in ("timeout", "connection", "apiconnection")):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int):
        if status == 429 or 500 <= status < 600:
            return True
    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if isinstance(code, int) and (code == 429 or 500 <= code < 600):
            return True
    code = getattr(exc, "code", None)
    if isinstance(code, int) and (code == 429 or 500 <= code < 600):
        return True
    return False


async def call_with_retry(
    func: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
) -> T:
    """Call ``func()`` with exponential-backoff retries on transient errors."""
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await func()
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= max_attempts or not _is_retryable(exc):
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = delay * (0.5 + random.random() * 0.5)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
