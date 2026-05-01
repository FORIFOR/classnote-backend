"""
STT Circuit Breaker

Tracks Google Cloud STT failures and provides fast fallback signals.
When the circuit is OPEN, new cloud STT requests are immediately rejected
so iOS can fall back to on-device transcription without waiting for timeouts.

States:
  CLOSED    -> Normal operation. STT requests pass through.
  OPEN      -> STT is considered down. Requests are immediately rejected.
  HALF_OPEN -> Testing recovery. One request is allowed through to probe.

Transitions:
  CLOSED    -> OPEN:      failure_count >= FAILURE_THRESHOLD
  OPEN      -> HALF_OPEN: cooldown_sec elapsed since last failure
  HALF_OPEN -> CLOSED:    probe request succeeds
  HALF_OPEN -> OPEN:      probe request fails
"""

import time
import logging
from enum import Enum

logger = logging.getLogger("app.stt_circuit_breaker")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class STTCircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_sec: float = 30.0,
        half_open_max_probes: int = 1,
    ):
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._failure_threshold = failure_threshold
        self._cooldown_sec = cooldown_sec
        self._half_open_max_probes = half_open_max_probes
        self._last_failure_at: float = 0.0
        self._half_open_probes = 0

    @property
    def state(self) -> CircuitState:
        # Auto-transition OPEN -> HALF_OPEN after cooldown
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_at >= self._cooldown_sec:
                self._state = CircuitState.HALF_OPEN
                self._half_open_probes = 0
                logger.info("[CircuitBreaker] OPEN -> HALF_OPEN (cooldown elapsed)")
        return self._state

    def is_available(self) -> bool:
        """Check if STT requests should be allowed."""
        current = self.state
        if current == CircuitState.CLOSED:
            return True
        if current == CircuitState.HALF_OPEN:
            if self._half_open_probes < self._half_open_max_probes:
                return True
            return False
        # OPEN
        return False

    def record_success(self) -> None:
        """Call after a successful STT stream connection."""
        if self._state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
            logger.info(f"[CircuitBreaker] {self._state.value} -> CLOSED (success)")
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._half_open_probes = 0

    def record_failure(self) -> None:
        """Call after an STT connection/init failure."""
        self._failure_count += 1
        self._last_failure_at = time.time()

        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            logger.warning("[CircuitBreaker] HALF_OPEN -> OPEN (probe failed)")
            return

        if self._failure_count >= self._failure_threshold:
            if self._state != CircuitState.OPEN:
                logger.warning(
                    f"[CircuitBreaker] CLOSED -> OPEN "
                    f"(failures={self._failure_count}/{self._failure_threshold})"
                )
            self._state = CircuitState.OPEN

    def record_probe(self) -> None:
        """Call when allowing a request through in HALF_OPEN state."""
        if self._state == CircuitState.HALF_OPEN:
            self._half_open_probes += 1

    def get_status(self) -> dict:
        """Return status dict for health/debug endpoints."""
        return {
            "state": self.state.value,
            "failureCount": self._failure_count,
            "failureThreshold": self._failure_threshold,
            "cooldownSec": self._cooldown_sec,
            "lastFailureAt": self._last_failure_at,
            "available": self.is_available(),
        }


# Module-level singleton
stt_circuit_breaker = STTCircuitBreaker()
