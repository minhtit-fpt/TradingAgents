"""HTTP transport for EDGAR.

Three things this module exists to guarantee, all of them about not losing
access to the only free source of US financial statements:

1. every request carries a descriptive ``User-Agent`` with a real contact
   address, as SEC's access policy requires;
2. requests are throttled below SEC's published 10 req/s ceiling;
3. transient failures back off instead of hammering.

A missing User-Agent raises at construction. There is deliberately no fallback
string: a plausible-looking fake agent is how a server IP gets blocked.
"""

import time
from typing import Any

import requests

from ..config import (
    EDGAR_MAX_RETRIES,
    EDGAR_REQUESTS_PER_SECOND,
    EDGAR_TIMEOUT_SECONDS,
    SEC_USER_AGENT,
)

# Status codes worth another attempt: rate limiting and server-side faults.
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_BACKOFF_BASE_SECONDS = 1.0


class SecRequestError(RuntimeError):
    """An EDGAR request failed after exhausting retries."""


class SecClient:
    """Rate-limited EDGAR HTTP client.

    One instance per run. The throttle is per-instance, so do not build several
    clients in one process and defeat the point of it.
    """

    def __init__(
        self,
        user_agent: str | None = None,
        requests_per_second: float | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
        session: requests.Session | None = None,
        sleep=time.sleep,
    ) -> None:
        agent = user_agent if user_agent is not None else SEC_USER_AGENT
        if not agent or "@" not in agent:
            raise ValueError(
                "SEC requires a User-Agent with a real contact email, e.g. "
                "VALUE_SEC_USER_AGENT='TradingAgents research you@example.com'. "
                f"Got {agent!r}."
            )
        rps = requests_per_second if requests_per_second is not None else EDGAR_REQUESTS_PER_SECOND
        if rps <= 0:
            raise ValueError(f"requests_per_second must be positive, got {rps}")

        self.user_agent = agent
        self.min_interval = 1.0 / rps
        self.max_retries = max_retries if max_retries is not None else EDGAR_MAX_RETRIES
        self.timeout = timeout if timeout is not None else EDGAR_TIMEOUT_SECONDS
        self.session = session or requests.Session()
        self._sleep = sleep
        self._last_request_at: float | None = None

    def get_json(self, url: str) -> Any:
        """GET ``url`` and parse it as JSON, throttled and retried."""
        response = self.get(url)
        try:
            return response.json()
        except ValueError as exc:
            raise SecRequestError(f"EDGAR returned non-JSON for {url}: {exc}") from exc

    def get(self, url: str) -> requests.Response:
        """GET ``url``, honouring the rate limit and retrying transient failures."""
        last_error = ""
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                response = self.session.get(
                    url,
                    headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    return response
                if response.status_code not in _RETRYABLE:
                    raise SecRequestError(f"EDGAR returned {response.status_code} for {url}")
                last_error = f"HTTP {response.status_code}"

            if attempt < self.max_retries - 1:
                self._sleep(_BACKOFF_BASE_SECONDS * (2**attempt))

        raise SecRequestError(
            f"EDGAR request failed after {self.max_retries} attempts ({last_error}) for {url}"
        )

    def _throttle(self) -> None:
        """Block until at least ``min_interval`` has passed since the last request."""
        if self._last_request_at is not None:
            wait = self.min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = time.monotonic()
