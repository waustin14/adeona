"""Per-task cap on paid API calls. Exhaustion is a normal, non-raising outcome."""

from __future__ import annotations


class CallBudget:
    def __init__(self, max_calls: int):
        if max_calls < 0:
            raise ValueError("max_calls must be >= 0")
        self.max_calls = max_calls
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.max_calls - self.used

    def try_consume(self) -> bool:
        """Reserve one call. Returns False (and reserves nothing) when exhausted."""
        if self.used >= self.max_calls:
            return False
        self.used += 1
        return True
