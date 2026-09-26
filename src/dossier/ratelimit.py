"""Request pacing, shared by every client that talks to an outside service.

EDGAR allows ten requests a second and enforces it; Yahoo's chart endpoint is unofficial
and gets far fewer. Each client sets its own limit, and none has to import another
client to get the mechanism.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    """A sliding-window limiter: at most `max_per_second` requests in any one second.

    The clock and sleep are injected so the pacing can be tested without spending real
    seconds on it.
    """

    def __init__(
        self,
        max_per_second: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_per_second < 1:
            raise ValueError("max_per_second must be at least 1")
        self.max_per_second = max_per_second
        self._clock = clock
        self._sleep = sleep
        self._recent: deque[float] = deque()

    def acquire(self) -> None:
        while True:
            now = self._clock()
            while self._recent and now - self._recent[0] >= 1.0:
                self._recent.popleft()
            if len(self._recent) < self.max_per_second:
                self._recent.append(now)
                return
            self._sleep(self._recent[0] + 1.0 - now)
