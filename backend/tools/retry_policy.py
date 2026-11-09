"""Shared tenacity retry policy for search and LLM calls.

Only transient failures (timeouts, connection errors, 429, 5xx) are retried.
Permanent ones (missing/invalid key, bad request, other 4xx) fail immediately, so a
misconfigured key does not burn the wall-clock budget on pointless backoff.

On a 429 the provider's requested wait is honoured, capped at MAX_RETRY_AFTER_SECONDS:
Groq sends a `retry-after` header; Gemini puts "retryDelay": "37s" in the error body.
A spent DAILY quota is different: waiting will not help today. is_quota_exhausted()
detects it (the error names a per-day quota, or asks to wait > QUOTA_WAIT_SECONDS);
such errors are not retried, so callers can switch model or give up immediately.
"""

import re
import time

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

MAX_RETRY_AFTER_SECONDS = 60  # longest per-minute wait we sit out (Groq TPM, Gemini RPM)
QUOTA_WAIT_SECONDS = 120      # asked to wait longer than this = a daily quota is spent
_backoff = wait_exponential(multiplier=0.5, min=0.5, max=4)

# Exception class names (across Tavily / Groq / Gemini SDKs) that will never succeed on retry.
_PERMANENT_NAMES = {
    "ConfigError",
    "InvalidAPIKeyError",
    "MissingAPIKeyError",
    "ForbiddenError",
    "BadRequestError",
    "UsageLimitExceededError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "UnprocessableEntityError",
}
_PER_DAY = re.compile(r"per ?day|PerDay|\((?:TPD|RPD)\)", re.I)
_BODY_DELAY = re.compile(r"retryDelay['\"]?\s*:\s*['\"](\d+(?:\.\d+)?)s|retry in (\d+(?:\.\d+)?)s", re.I)


class ConfigError(RuntimeError):
    """Raised when something is not configured (e.g. API key missing)."""


def _status_code(exc: BaseException) -> int | None:
    """HTTP status across SDKs: Gemini `.code`, Groq `.status_code`, requests `.response.status_code`."""
    for value in (getattr(exc, "code", None), getattr(exc, "status_code", None)):
        if isinstance(value, int):
            return value
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def _raw_retry_after(exc: BaseException | None) -> float | None:
    resp = getattr(exc, "response", None)
    value = resp.headers.get("retry-after") if resp is not None and hasattr(resp, "headers") else None
    try:
        if value is not None:
            return float(value)
    except (TypeError, ValueError):
        pass
    m = _BODY_DELAY.search(str(exc)) if exc is not None else None
    return float(m.group(1) or m.group(2)) if m else None


def is_quota_exhausted(exc: BaseException) -> bool:
    """A rate limit that will not clear soon, e.g. Groq "tokens per day (TPD)" or a Gemini PerDay quota."""
    if _status_code(exc) == 429 and _PER_DAY.search(str(exc)):
        return True
    after = _raw_retry_after(exc)
    return after is not None and after > QUOTA_WAIT_SECONDS


def is_transient(exc: BaseException) -> bool:
    if type(exc).__name__ in _PERMANENT_NAMES or is_quota_exhausted(exc):
        return False
    code = _status_code(exc)
    if code is not None and 400 <= code < 500:
        return code == 429
    return True


def _wait(retry_state) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    after = _raw_retry_after(exc)
    return min(after, MAX_RETRY_AFTER_SECONDS) if after is not None else _backoff(retry_state)


STOP_CHECK_SECONDS = 0.25


def _sleep(seconds: float) -> None:
    """Wait between retries in short steps, so a run the user paused stops now instead of after a rate-limit
    wait of up to MAX_RETRY_AFTER_SECONDS (measured live: about 25 s). Outside a run it is a plain sleep."""
    from core import live  # imported here: core.llm imports this module

    end = time.monotonic() + seconds
    while True:
        live.raise_if_stopped()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(STOP_CHECK_SECONDS, left))


def with_retries(max_retries: int):
    """Decorator: up to `max_retries` retries on transient errors, waiting as the provider asks."""
    return retry(
        retry=retry_if_exception(is_transient),
        stop=stop_after_attempt(max_retries + 1),
        wait=_wait,
        sleep=_sleep,
        reraise=True,
    )
