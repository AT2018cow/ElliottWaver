"""Bounded REST requests with feed-independent authentication handling."""

import time
from email.utils import parsedate_to_datetime

import requests

from elliottwaver.config import DataConfig
from elliottwaver.errors import SourceError


class HTTPClient:
    def __init__(self, config: DataConfig, headers=None, session=None):
        self.config = config
        self.headers = headers or {}
        self.session = session or requests.Session()
        self.started = time.monotonic()
        self.last_request = 0.0

    def get_json(self, url: str, params=None):
        for attempt in range(self.config.max_attempts):
            elapsed = time.monotonic() - self.started
            remaining = self.config.total_fetch_deadline_seconds - elapsed
            if remaining <= 0:
                raise SourceError("Market-data request deadline exceeded")
            pause = max(
                0, self.last_request + 1 / self.config.max_requests_per_second - time.monotonic()
            )
            if pause >= remaining:
                raise SourceError("Market-data rate limit exceeds the remaining deadline")
            time.sleep(pause)
            self.last_request = time.monotonic()
            timeout = min(self.config.request_timeout_seconds, max(0.1, remaining - pause))
            try:
                response = self.session.get(
                    url, params=params, headers=self.headers, timeout=timeout
                )
                if response.status_code in (401, 403):
                    raise SourceError(
                        f"SOURCE_FORBIDDEN: HTTP {response.status_code}; check credentials and feed entitlement"
                    )
                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt + 1 == self.config.max_attempts:
                        raise SourceError(
                            f"Market-data source returned HTTP {response.status_code}"
                        )
                    retry = response.headers.get("Retry-After", "")
                    try:
                        delay = float(retry)
                    except ValueError:
                        try:
                            delay = parsedate_to_datetime(retry).timestamp() - time.time()
                        except (ValueError, TypeError, OverflowError):
                            delay = 0.5 * 2**attempt
                    remaining = self.config.total_fetch_deadline_seconds - (
                        time.monotonic() - self.started
                    )
                    if delay >= remaining:
                        raise SourceError("Source requested a retry beyond the total deadline")
                    time.sleep(max(0, delay))
                    continue
                if response.status_code != 200:
                    raise SourceError(f"Market-data source returned HTTP {response.status_code}")
                return response.json()
            except requests.RequestException as exc:
                if attempt + 1 == self.config.max_attempts:
                    raise SourceError(
                        f"Market-data connection failed ({type(exc).__name__})"
                    ) from exc
            except ValueError as exc:
                raise SourceError("Market-data source returned malformed JSON") from exc
        raise SourceError("Market-data retry budget exhausted")
