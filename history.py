"""Metric history and the optional CSV trace.

`resample` is the interesting part: the row graphs are a few hundred pixels
wide while the buffer holds a minute of samples, so something has to be
thrown away, and uniform sampling throws away spikes.
"""

from __future__ import annotations

import logging
import math
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
        """Record one normalised sample; drop the ones that are not readings.

        `None` means "not measured" and is not recorded. NaN is treated the
        same way, and deliberately not clamped: `min(1.0, max(0.0, nan))`
        evaluates to `0.0`, because `max` discards the NaN comparison, so
        clamping alone would file a NaN reading as a real 0% data point and
        draw it as one. A metric that failed to report is not a metric that
        reported zero. `inf` needs no special case -- clamping it to 1.0 is
        the honest reading of an over-range value.
        """
        if value is None or math.isnan(value):
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
        self._header_checked = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch(exist_ok=True)
        except OSError as exc:
            logger.warning("history log unavailable, staying off: %s", exc)
            return False
        self._enabled = True
        return True

    def disable(self) -> None:
        self._switch_off()

    def _switch_off(self) -> None:
        """Stop writing, and forget what was decided about the file.

        The verdict on the header belongs to the file, not to this object, so
        it has to be forgotten as well. Leaving it behind let the next
        enable() -- which is what the tray menu does -- skip the check and
        append this build's columns to a trace whose header belonged to another
        build, which is the exact schema mixing the check exists to prevent.
        """
        self._enabled = False
        self._has_header = False
        self._header_checked = False

    def write(self, snapshot) -> None:
        if not self._enabled:
            return
        values = snapshot.as_dict()
        try:
            if not self._header_checked:
                if not self._adopt_existing_header(values):
                    self._switch_off()
                    return
                # Set only now that the check has run to completion: marking it
                # done first left it marked when adopting raised part-way
                # through, and the retry after a re-enable skipped the check.
                self._header_checked = True
            with self._path.open("a", encoding="utf-8") as handle:
                # Both strings come from one dict, so they agree within a
                # single write. Across runs the header is checked once, on the
                # first write, because only then is this build's column list
                # knowable.
                if not self._has_header:
                    handle.write(",".join(values.keys()) + "\n")
                    self._has_header = True
                handle.write(",".join(_csv(v) for v in values.values()) + "\n")
        except OSError as exc:
            logger.warning("history log write failed, switching off: %s", exc)
            self._switch_off()

    def _adopt_existing_header(self, values: dict[str, float | None]) -> bool:
        """Decide once whether a trace written by an earlier run can be extended.

        The columns come from the snapshot, so they are only knowable here. A
        trace whose header does not match them was written by a build with
        other columns, and appending to it would put rows of one width under a
        header of another: the arity of every row would look fine while each
        column after the first was reading the wrong value. Such a file is
        rotated aside -- kept, never overwritten -- and the new trace starts
        with a correct header.

        Returns False when the file must not be written to at all: it holds
        columns this build does not write and it could not be moved aside, so
        appending would mix the two schemas in one file for good.
        """
        if not self._path.exists():
            return True  # nothing there yet; the append below starts a new trace
        expected = ",".join(values.keys())
        try:
            # errors="replace" because a trace saved as UTF-16 or written as
            # cp1251 is not UTF-8, and a decode failure is a ValueError rather
            # than an OSError -- it used to escape write() into the sampling
            # tick. Garbled bytes cannot match an ASCII header, so they take
            # the rotate branch: the file is kept, never appended to.
            with self._path.open("r", encoding="utf-8", errors="replace") as handle:
                first = handle.readline().strip()
        except OSError as exc:
            logger.warning("history log unreadable, rotating it aside: %s", exc)
            first = None
        if first is not None and first == expected:
            self._has_header = True
            return True
        if first == "":
            return True  # empty file: the append below writes the header
        rotated = self._free_rotation_path()
        try:
            self._path.replace(rotated)
        except OSError as exc:
            logger.warning(
                "history log has foreign columns and cannot be rotated, switching off: %s", exc
            )
            return False
        logger.warning(
            "history log columns do not match this build, moved to %s", rotated
        )
        return True

    def _free_rotation_path(self) -> Path:
        number = 0
        while True:
            number += 1
            candidate = self._path.with_name(f"{self._path.name}.{number}")
            if not candidate.exists():
                return candidate


def _csv(value: float | None) -> str:
    return "" if value is None else f"{value:g}"
