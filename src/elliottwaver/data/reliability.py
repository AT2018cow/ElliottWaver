"""Small reliability helpers shared by free data adapters."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

import requests

from elliottwaver.errors import SourceError

T = TypeVar("T")
_RETRY_STATUS = {429, 500, 502, 503, 504}


def retry_call(
    function: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    backoff: float = 2.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    last: BaseException | None = None
    for index in range(attempts):
        try:
            return function()
        except retry_on as exc:
            last = exc
            if index + 1 >= attempts:
                raise
            time.sleep(base_delay * (backoff**index))
    assert last is not None
    raise last


def request_json(
    method: str,
    url: str,
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    timeout: tuple[float, float] = (10, 20),
    **kwargs,
) -> dict:
    for index in range(attempts):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            if response.status_code in _RETRY_STATUS:
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else base_delay * (2**index)
                )
                if index + 1 < attempts:
                    time.sleep(delay)
                    continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("response is not a JSON object")
            return payload
        except (requests.RequestException, ValueError):
            if index + 1 >= attempts:
                raise SourceError("HTTP_REQUEST_FAILED: 无法取得有效行情响应") from None
            time.sleep(base_delay * (2**index))
    raise ValueError("attempts must be positive")
