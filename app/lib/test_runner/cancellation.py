"""Cooperative signal cancellation; signal handlers never dispatch network IO."""
from __future__ import annotations

import contextlib
import math
import signal
import threading

from .common import DispatchStopped


@contextlib.contextmanager
def response_deadline(seconds: float):
    """Bound complete synchronous transport reads, including trickling streams.

    A worker process must invoke this on its main thread with no existing alarm;
    refusing unsupported execution contexts prevents silently unbounded requests.
    """
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Response deadline must be finite and positive")
    if threading.current_thread() is not threading.main_thread() or signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ValueError("Request requires an unused main-thread deadline")
    previous = signal.getsignal(signal.SIGALRM)
    def expired(_signum, _frame):
        raise TimeoutError("Overall response deadline exceeded")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class CancellationContext:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def request(self, reason: str = "cancelled") -> None:
        if not self.cancelled:
            self.reason = str(reason)
            self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise DispatchStopped(self.reason or "cancelled")

    @contextlib.contextmanager
    def install_signal_handlers(self):
        """Explicit opt-in on the main thread; always restore prior handlers."""
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Signal handlers must be installed on the main thread")
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        def receive(signum, _frame):
            self.request(signal.Signals(signum).name)
        try:
            for sig in previous:
                signal.signal(sig, receive)
            yield self
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    @staticmethod
    def shutdown_grace_seconds(remaining_request_seconds: float, cleanup_deadline_seconds: float) -> float:
        return max(0.0, remaining_request_seconds) + max(0.0, cleanup_deadline_seconds) + 5.0
