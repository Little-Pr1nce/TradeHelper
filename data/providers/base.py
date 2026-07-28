"""Small provider primitives. Network clients remain replaceable at the boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from time import sleep
from typing import Callable, Generic, Protocol, TypeVar

from contracts.enums import ProviderStatus
from contracts.providers import ProviderAttempt, ProviderResult
from contracts.market_data import ensure_utc

T = TypeVar("T")


class ProviderClient(Protocol[T]):
    """A provider returns a typed result and never raises expected transport failures."""

    name: str

    def fetch(self, *args: object, **kwargs: object) -> ProviderResult[T]: ...


RetryScheduler = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class RetryingClient(Generic[T]):
    """Bounded retry policy usable by concrete network adapters and test fakes."""

    name: str
    invoke: Callable[[], T]
    now: Callable[[], datetime]
    classify_error: Callable[[Exception], ProviderStatus]
    scheduler: RetryScheduler = sleep
    max_attempts: int = 3

    def fetch(self) -> ProviderResult[T]:
        attempts: list[ProviderAttempt] = []
        last_status = ProviderStatus.UNAVAILABLE
        for number in range(self.max_attempts):
            started = ensure_utc(self.now(), "provider started_at")
            try:
                value = self.invoke()
            except Exception as exc:  # concrete adapters classify only expected transport errors.
                finished = ensure_utc(self.now(), "provider finished_at")
                status = self.classify_error(exc)
                attempts.append(ProviderAttempt(self.name, status, started, finished, type(exc).__name__, str(exc)))
                last_status = status
                if status not in {ProviderStatus.TIMEOUT, ProviderStatus.RATE_LIMITED, ProviderStatus.UNAVAILABLE}:
                    break
                if number + 1 < self.max_attempts:
                    retry_after = getattr(exc, "retry_after", None)
                    try:
                        delay = min(float(retry_after), 30.0) if retry_after is not None else float(1 << number)
                    except (TypeError, ValueError):
                        delay = float(1 << number)
                    self.scheduler(delay)
                continue
            finished = ensure_utc(self.now(), "provider finished_at")
            if value is None:
                attempts.append(ProviderAttempt(self.name, ProviderStatus.EMPTY, started, finished))
                return ProviderResult.failure(ProviderStatus.EMPTY, finished, tuple(attempts))
            attempts.append(ProviderAttempt(self.name, ProviderStatus.OK, started, finished))
            return ProviderResult.success(value, self.name, finished, tuple(attempts))
        return ProviderResult.failure(last_status, ensure_utc(self.now(), "provider fetched_at"), tuple(attempts))


def unavailable_result(provider: str, now: datetime, status: ProviderStatus = ProviderStatus.UNAVAILABLE) -> ProviderResult[T]:
    timestamp = ensure_utc(now, "provider timestamp")
    attempt = ProviderAttempt(provider, status, timestamp, timestamp)
    return ProviderResult.failure(status, timestamp, (attempt,))
