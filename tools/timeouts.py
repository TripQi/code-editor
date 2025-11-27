from __future__ import annotations

import concurrent.futures
from typing import Callable, TypeVar

T = TypeVar("T")


def with_timeout(func: Callable[[], T], timeout_seconds: float | None, timeout_message: str) -> T:
    if timeout_seconds is None:
        return func()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError as exc:  # pragma: no cover - simple passthrough
            future.cancel()
            raise TimeoutError(timeout_message) from exc
