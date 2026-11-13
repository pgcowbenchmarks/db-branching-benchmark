"""Timing utilities: high-resolution measurement and robust statistics."""
from __future__ import annotations

import statistics
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


@contextmanager
def stopwatch():
    """Context manager yielding a one-element list; result[0] is elapsed seconds.

    Uses perf_counter (monotonic, highest resolution available).
    """
    result = [0.0]
    start = time.perf_counter()
    try:
        yield result
    finally:
        result[0] = time.perf_counter() - start


def timed(fn, *args, **kwargs) -> tuple[float, object]:
    """Run fn(*args, **kwargs); return (elapsed_seconds, return_value)."""
    start = time.perf_counter()
    value = fn(*args, **kwargs)
    return time.perf_counter() - start, value


@dataclass
class Samples:
    """A collection of timing samples with median and min-max range accessors."""

    values: list[float] = field(default_factory=list)

    def add(self, v: float) -> None:
        self.values.append(v)

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def median(self) -> float | None:
        return statistics.median(self.values) if self.values else None

    @property
    def min(self) -> float | None:
        return min(self.values) if self.values else None

    @property
    def max(self) -> float | None:
        return max(self.values) if self.values else None

    @property
    def mean(self) -> float | None:
        return statistics.fmean(self.values) if self.values else None

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "median_s": self.median,
            "min_s": self.min,
            "max_s": self.max,
            "mean_s": self.mean,
            "raw_s": list(self.values),
        }
