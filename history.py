"""Metric history and the optional CSV trace.

`resample` is the interesting part: the row graphs are a few hundred pixels
wide while the buffer holds a minute of samples, so something has to be
thrown away, and uniform sampling throws away spikes.
"""

from __future__ import annotations

import logging
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QPointF

from theme import HISTORY_LEN

logger = logging.getLogger("widget.history")


class History:
    """Ring buffer of one normalised metric, clamped to 0.0-1.0."""

    __slots__ = ("_values",)

    def __init__(self, maxlen: int = HISTORY_LEN) -> None:
        self._values: deque[float] = deque(maxlen=maxlen)

    def __len__(self) -> int:
        return len(self._values)

    def append(self, value: float | None) -> None:
        if value is None:
            return
        self._values.append(min(1.0, max(0.0, value)))

    def values(self) -> list[float]:
        return list(self._values)

    def resampled(self, width: int) -> list[QPointF]:
        return resample(self.values(), width)


def resample(points: list[float], width: int) -> list[QPointF]:
    """Fit points into `width` columns, keeping each column's extreme.

    Uniform sampling would drop spikes: with more samples than pixels a
    one-sample spike is easily averaged away, and the graph would lie about
    exactly the moment it exists to reveal. Instead every column after the
    first picks the value furthest from the point drawn just before it.

    The trade-off is on purpose. Measuring "furthest" from the previous
    point means taking the largest step in the window, so the line breaks
    its continuity at a spike in order to keep the spike. Column 0 has no
    predecessor to measure a step from, so it falls back to plain
    `max(window)`: with nothing to stay continuous with, the column's peak
    is the only spike-preserving choice left.

    Chosen values are copied out of the window, never interpolated, so the
    graph shows measured numbers only. Input is not clamped here --
    `History.append` clamps before anything reaches the buffer.
    """
    if width <= 0 or not points:
        return []
    n = len(points)
    if n == 1:
        return [QPointF(0.0, points[0])]
    if n <= width:
        step = (width - 1) / (n - 1)
        return [QPointF(i * step, points[i]) for i in range(n)]

    out: list[QPointF] = []
    previous: float | None = None
    for column in range(width):
        lo = column * n // width
        hi = max(lo + 1, (column + 1) * n // width)
        window = points[lo:hi]
        chosen = max(window) if previous is None else max(window, key=lambda v: abs(v - previous))
        out.append(QPointF(float(column), chosen))
        previous = chosen
    return out


class HistoryLog:
    """Appends one CSV row per tick. Off by default; enabled from the tray."""

    def __init__(self, path: Path | str = "metrics_history.log") -> None:
        self._path = Path(path)
        self._enabled = False
        self._has_header = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> bool:
        try:
            self._has_header = self._path.exists() and self._path.stat().st_size > 0
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch(exist_ok=True)
        except OSError as exc:
            logger.warning("history log unavailable, staying off: %s", exc)
            return False
        self._enabled = True
        return True

    def disable(self) -> None:
        self._enabled = False

    def write(self, snapshot) -> None:
        if not self._enabled:
            return
        values = snapshot.as_dict()
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                # Both strings come from one dict, so they agree within a
                # single write. Across runs they can still disagree: a file
                # left by a build with other columns is trusted as-is,
                # because `enable()` cannot know this build's column list
                # until the first write, and rewriting a user's existing
                # log to hide that would trade data for cosmetics.
                if not self._has_header:
                    handle.write(",".join(values.keys()) + "\n")
                    self._has_header = True
                handle.write(",".join(_csv(v) for v in values.values()) + "\n")
        except OSError as exc:
            logger.warning("history log write failed, switching off: %s", exc)
            self._enabled = False


def _csv(value: float | None) -> str:
    return "" if value is None else f"{value:g}"
