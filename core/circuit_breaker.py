"""Lightweight credential-scoped circuit breakers for outbound GitHub calls.

The breaker only reacts to transient transport/service failures. Authentication
and validation failures do not count because they are not downstream-service
instability. State is keyed by a SHA-256 credential fingerprint, never by the
raw token, and is process-local.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass


class CircuitOpenError(Exception):
    """Raised when a circuit is open and the call is rejected locally."""

    def __init__(self, name: str, retry_after: float) -> None:
        self.name = name
        self.retry_after = max(0.0, retry_after)
        super().__init__(f"Circuit breaker '{name}' is open; retry after {self.retry_after:.1f}s")


@dataclass
class CircuitBreaker:
    """Small closed/open circuit breaker with a half-open probe state."""

    name: str
    failure_threshold: int = 5
    reset_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        self.failure_threshold = max(1, int(self.failure_threshold))
        self.reset_timeout_seconds = max(1.0, float(self.reset_timeout_seconds))
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_probe = False
        self._lock = threading.Lock()

    def before_call(self) -> None:
        """Raise when the circuit is open; otherwise permit the call."""
        with self._lock:
            if self._opened_at <= 0:
                return
            elapsed = time.monotonic() - self._opened_at
            if elapsed < self.reset_timeout_seconds:
                raise CircuitOpenError(self.name, self.reset_timeout_seconds - elapsed)
            if self._half_open_probe:
                raise CircuitOpenError(self.name, 1.0)
            self._half_open_probe = True

    def record_success(self) -> None:
        """Close the circuit after a successful downstream response."""
        with self._lock:
            self._failures = 0
            self._opened_at = 0.0
            self._half_open_probe = False

    def record_failure(self) -> None:
        """Count one transient failure and open the circuit at the threshold."""
        with self._lock:
            self._failures += 1
            self._half_open_probe = False
            if self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()

    def snapshot(self) -> dict[str, float | int | bool]:
        """Return a token-free state snapshot for diagnostics/tests."""
        with self._lock:
            return {
                "open": self._opened_at > 0,
                "failures": self._failures,
                "half_open_probe": self._half_open_probe,
                "opened_for_seconds": round(time.monotonic() - self._opened_at, 3) if self._opened_at else 0.0,
            }


_BREAKERS_LOCK = threading.Lock()
_BREAKERS: dict[str, CircuitBreaker] = {}
_MAX_BREAKERS = 256


def github_circuit_breaker(token: str) -> CircuitBreaker:
    """Return the shared GitHub API breaker for a credential fingerprint.

    The process-local cache is bounded so multi-user HTTP deployments cannot
    grow memory without limit by presenting many distinct bearer tokens.
    """
    from core.config import get_settings

    settings = get_settings()
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
    name = f"github-api:{fingerprint[:16]}"
    with _BREAKERS_LOCK:
        breaker = _BREAKERS.get(name)
        if breaker is not None:
            _BREAKERS[name] = _BREAKERS.pop(name)
            return breaker
        if len(_BREAKERS) >= _MAX_BREAKERS:
            _BREAKERS.pop(next(iter(_BREAKERS)))
        breaker = CircuitBreaker(
            name,
            failure_threshold=settings.circuit_breaker_failures,
            reset_timeout_seconds=settings.circuit_breaker_reset_seconds,
        )
        _BREAKERS[name] = breaker
        return breaker


def reset_circuit_breakers() -> None:
    """Reset process-local breakers (intended for tests)."""
    with _BREAKERS_LOCK:
        _BREAKERS.clear()


__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "github_circuit_breaker",
    "reset_circuit_breakers",
]
