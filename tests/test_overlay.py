"""Panel window tests: placement, dragging, input, and the paint delegation.

Nothing here calls show(). A test run must leave no window on screen. Every
thing the panel needs works while it is hidden: render() drives paintEvent
without a native window, QWidget.screen() falls back to the primary screen when
the widget has no window handle yet, and hideEvent can be delivered by calling
it directly -- Qt never delivers it to a widget that was never shown.

The pixels under test are the widget's own: render() redirects the painter that
paintEvent creates into a QImage the test owns, which is the only way to see
what the panel draws without putting a window on screen.
"""

import inspect
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import (
    QColor,
    QContextMenuEvent,
    QHideEvent,
    QImage,
    QMouseEvent,
    QPainter,
    QWheelEvent,
)

import overlay
import theme
from metrics import Snapshot
from overlay import MonitorPanel
from painter import paint
from settings import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent

# Driving a failing render() on purpose can end the interpreter: PyQt calls
# qFatal() for an exception escaping a reimplemented virtual method, and if the
# render guard ever regresses there is no failure to report -- the run simply
# stops where that test is. Ordering cannot help, because the first such test in
# the file is the one that dies. So those tests run in a child process, which
# test_a_failed_frame_does_not_take_the_process_with_it starts and asserts on; a
# regression then costs one readable failure instead of every result after it.
#
# This list is built, not written. A test that renders frames which raise is
# decorated with @renders_failing_frames, which appends its name here as the
# module is imported -- so the canary's list is complete before a single test
# runs -- and marks it as child-process-only. Both halves matter: the decorator
# on its own is a convention somebody can forget, so render_panel() and
# failing_render() also refuse to work for a caller that is not on the list.
# Without that second half, adding one test and forgetting the decorator
# reinstated the abort with nothing to warn about it, which is exactly what
# happened twice.
ABORT_CANARY_CHILD = "MONITOR_ABORT_CANARY_CHILD"

ABORT_PRONE_TESTS: list[str] = []

_real_paint = paint


def abort_prone(func):
    """Mark one test as child-process-only."""
    return pytest.mark.skipif(
        os.environ.get(ABORT_CANARY_CHILD) != "1",
        reason="renders frames which raise: runs in the child process, and the "
               "parent asserts on its exit code",
    )(func)


def renders_failing_frames(func):
    """The only way to write a test that renders frames which raise.

    Registers the test in ABORT_PRONE_TESTS at import time and hands it the
    child-process-only marker. Install the failing renderer with failing_render()
    and draw through render_panel(); both of those check this registration.
    """
    ABORT_PRONE_TESTS.append(func.__name__)
    return abort_prone(func)


def _calling_test_name():
    """The nearest enclosing test function's name, or None off the test path."""
    frame = inspect.currentframe()
    while frame is not None:
        name = frame.f_code.co_name
        if name.startswith("test_"):
            return name
        frame = frame.f_back
    return None


def _require_registered_caller(what):
    caller = _calling_test_name()
    if caller not in ABORT_PRONE_TESTS:
        raise RuntimeError(
            f"{caller} {what} with a renderer that raises, and is not in "
            f"ABORT_PRONE_TESTS. Decorate it with @renders_failing_frames: an "
            f"exception escaping paintEvent reaches Qt's qFatal(), so running it "
            f"here would end the interpreter and lose every result after it, "
            f"instead of failing one test."
        )


@contextmanager
def failing_render(monkeypatch, fault):
    """Install `fault` as the renderer, for a registered test only."""
    _require_registered_caller("renders frames")
    with monkeypatch.context() as patch:
        patch.setattr(overlay, "paint", fault)
        yield


def make_panel():
    """A panel that has never been on screen."""
    return MonitorPanel(Settings())


@contextmanager
def recording_painters(monkeypatch):
    """Keep hold of every painter the widget opens, so it can be inspected.

    An unended painter is active on the widget's paint device, and PyQt's
    garbage collector ends an abandoned one as the frame unwinds -- so the
    pixels cannot show the bug, and something has to keep the reference.
    overlay imports QPainter by name, so patching the module attribute is
    enough to catch the ones it opens.
    """
    opened = []

    class RecordingPainter(QPainter):
        def __init__(self, device):
            super().__init__(device)
            opened.append(self)

    monkeypatch.setattr(overlay, "QPainter", RecordingPainter)
    yield opened


def active_painters(opened):
    return [index for index, painter in enumerate(opened) if painter.isActive()]


def mouse_event(kind, panel, global_at, button, buttons):
    """Build the event the way Qt builds one, from a global position.

    QMouseEvent takes QPointF in PyQt6, not QPoint; passing a QPoint raises
    TypeError. The local position is derived from `global_at` because the panel
    tracks the cursor globally: a move event whose global position repeats the
    press moves nothing at all, so a drag test written that way passes without
    ever dragging.
    """
    return QMouseEvent(
        kind,
        QPointF(global_at - panel.pos()),
        QPointF(global_at),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )


def wheel(dy):
    return QWheelEvent(
        QPointF(50.0, 50.0),
        QPointF(50.0, 50.0),
        QPoint(0, 0),
        QPoint(0, dy),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )


def blank_canvas():
    image = QImage(theme.CANVAS_W, theme.CANVAS_H, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0, 0))
    return image


def render_panel(panel):
    """Paint the widget into an image, hidden, without repainting the screen.

    The one door to widget.render() in this file, because that is what can end
    the interpreter: an exception raised by the renderer under test escapes
    paintEvent and reaches qFatal(). If the renderer has been swapped for
    something that may raise, the calling test has to be registered in
    ABORT_PRONE_TESTS; otherwise this raises instead, which costs one test and
    says what to do about it.
    """
    if overlay.paint is not _real_paint:
        _require_registered_caller("renders frames")
    image = blank_canvas()
    panel.render(image)
    return image


def fresh_paint(panel):
    """One paint() call on a painter of its own: what the widget should draw."""
    image = blank_canvas()
    canvas_painter = QPainter(image)
    try:
        paint(canvas_painter, panel._snapshot, panel._histories, panel.current_alpha())
    finally:
        canvas_painter.end()
    return image


class Clock:
    """A monotonic clock the test owns.

    The paint-log floor is minutes long by design, so no test may wait for it.
    Replacing overlay's `time` puts the frame cadence and the floor on the same
    pretend timeline.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def tick(self, frames: int = 1) -> None:
        self.now += theme.TICK_MS / 1000.0 * frames


def failing_paint(message):
    def raiser(*args, **kwargs):
        raise RuntimeError(message)

    return raiser


def fault_from_probe_a(*args, **kwargs):
    """One raise site."""
    raise RuntimeError("probe A")


def fault_from_probe_b(*args, **kwargs):
    """A second raise site, on its own line: a different traceback entirely."""
    raise RuntimeError("probe B")


def fault_with_value(value):
    """The same site, every time, but a message that changes on every frame."""

    def raiser(*args, **kwargs):
        raise ValueError(f"expected {value}")

    return raiser


def painting_nothing(*args, **kwargs):
    """A renderer that draws nothing and raises nothing: a frame that worked."""


def fault_on_frame(scenario: str, index: int):
    """(does this frame fail, what does it raise) for one frame of a scenario.

    These are the ways a render can keep failing. All of them wrote a record per
    frame under some version of this logger; all of them now write what the
    floor allows in the time that passed. The intermittent one has to deliver
    real healthy frames, not merely skip a frame: a clean frame is what resets
    the widget's own state, so skipping one would not reproduce the fault.
    """
    if scenario == "persistent":
        return True, failing_paint("same fault")
    if scenario == "alternating":
        return True, (fault_from_probe_a if index % 2 == 0 else fault_from_probe_b)
    if scenario == "changing_message":
        return True, fault_with_value(round(index * 0.7, 1))
    if scenario == "new_raise_site":
        # A different function: a new site mid-window, which the identity memo
        # used to treat as news worth interrupting for.
        return True, (fault_from_probe_a if index < 3 else fault_from_probe_b)
    if scenario == "intermittent":
        # One bad frame, one clean frame, forever. The panel looks healthy half
        # the time, which is the shape a marginal renderer failure really has.
        return True, (failing_paint("bad frame") if index % 2 == 0 else painting_nothing)
    raise ValueError(f"unknown scenario: {scenario}")


SCENARIOS = (
    "persistent",
    "alternating",
    "changing_message",
    "new_raise_site",
    "intermittent",
)

# The chosen log floor, restated here on purpose. The bounds below are computed
# from this value rather than from overlay.PAINT_ERROR_LOG_INTERVAL, so moving
# the interval -- raising it far enough to mute a fault, or lowering it enough
# to let the log grow -- fails instead of quietly redefining "correct".
EXPECTED_LOG_FLOOR = 300.0


def max_channel_delta(a, b):
    worst = 0
    for y in range(a.height()):
        for x in range(a.width()):
            left, right = a.pixelColor(x, y), b.pixelColor(x, y)
            worst = max(
                worst,
                abs(left.red() - right.red()),
                abs(left.green() - right.green()),
                abs(left.blue() - right.blue()),
                abs(left.alpha() - right.alpha()),
            )
    return worst


def test_the_panel_is_the_canvas_size_and_not_the_panel_size():
    """The widget carries the panel *and* the margin its shadow bleeds into.

    painter.paint() translates by theme.BLEED and draws the drop shadow outside
    panel_rect(), so a widget sized to theme.WIDTH x theme.HEIGHT clips the
    halo at its own edge and the panel reads as a hard wall -- the defect the
    golden images had before the canvas grew. The equality is on the canvas, so
    a regression to the panel size fails here rather than looking like a
    slightly tighter shadow nobody notices.
    """
    panel = make_panel()
    assert panel.width() == theme.CANVAS_W == theme.WIDTH + 2 * theme.BLEED
    assert panel.height() == theme.CANVAS_H == theme.HEIGHT + 2 * theme.BLEED


def test_panel_does_not_steal_focus():
    panel = make_panel()
    assert panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


def test_snapshot_feeds_the_histories():
    panel = make_panel()
    for pct in (10.0, 50.0, 90.0):
        panel.apply_snapshot(Snapshot(cpu_pct=pct, ram_used_gb=1.0, ram_total_gb=2.0))
    assert len(panel._histories["cpu_pct"]) == 3
    assert panel._histories["cpu_pct"].values()[-1] == 0.9


def test_unmeasured_rows_do_not_extend_the_history():
    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=10.0))
    panel.apply_snapshot(Snapshot(cpu_pct=None))
    assert len(panel._histories["cpu_pct"]) == 1


def test_alpha_is_clamped():
    panel = make_panel()
    panel.set_alpha(5.0)
    assert panel.current_alpha() == theme.MAX_ALPHA
    panel.set_alpha(-2.0)
    assert panel.current_alpha() == theme.MIN_ALPHA


def test_the_wheel_steps_the_alpha_within_its_range():
    panel = make_panel()
    panel.set_alpha(theme.DEFAULT_ALPHA)

    panel.wheelEvent(wheel(120))
    assert panel.current_alpha() == pytest.approx(
        theme.DEFAULT_ALPHA + theme.WHEEL_ALPHA_STEP
    )

    for _ in range(20):
        panel.wheelEvent(wheel(120))
    assert panel.current_alpha() == theme.MAX_ALPHA

    panel.wheelEvent(wheel(-120))
    assert panel.current_alpha() < theme.MAX_ALPHA


def test_a_wheel_with_no_vertical_delta_leaves_the_alpha_alone():
    """A sideways swipe is not an opacity gesture.

    angleDelta().y() is 0 for a horizontal wheel notch and for a purely
    horizontal trackpad swipe, so `> 0` took the else branch and spent a whole
    step on a gesture that had no vertical component at all: 0.80 became 0.75
    under the user's finger while they were scrolling sideways.
    """
    panel = make_panel()
    panel.set_alpha(theme.DEFAULT_ALPHA)

    sideways = QWheelEvent(
        QPointF(50.0, 50.0),
        QPointF(50.0, 50.0),
        QPoint(120, 0),
        QPoint(0, 0),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )
    panel.wheelEvent(sideways)

    assert sideways.angleDelta().y() == 0, "the fixture is not horizontal any more"
    assert panel.current_alpha() == theme.DEFAULT_ALPHA


def test_drag_moves_the_panel():
    panel = make_panel()
    panel.move(400, 300)
    start = panel.pos()

    # Pressed 10 px inside the panel, then the cursor travels +60, +40. The
    # global positions differ, which is the whole of what a drag is.
    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    panel.mouseMoveEvent(mouse_event(
        QMouseEvent.Type.MouseMove, panel, QPoint(470, 350),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
    ))
    panel.mouseReleaseEvent(mouse_event(
        QMouseEvent.Type.MouseButtonRelease, panel, QPoint(470, 350),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
    ))

    assert panel.pos() == start + QPoint(60, 40)
    # The drop position is remembered so a restart comes back to the same spot.
    assert panel._settings.x == panel.x()
    assert panel._settings.y == panel.y()


def test_release_clears_the_drag_state():
    """The whole of what a release has to do: arm nothing for the next drag.

    Nothing else in the widget clears _drag_origin, so a release that stopped
    doing this left every later move able to drag a panel nobody is holding.
    """
    panel = make_panel()
    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    assert panel._drag_origin is not None, "the press did not arm a drag at all"

    panel.mouseReleaseEvent(mouse_event(
        QMouseEvent.Type.MouseButtonRelease, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
    ))

    assert panel._drag_origin is None


def test_release_puts_the_cursor_back_to_open_hand():
    """The grab cursor is the panel's own state and has to be given back.

    An open-hand cursor that stayed a closed hand after the drop says the
    panel is still being carried, which it is not.
    """
    panel = make_panel()
    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    assert panel.cursor().shape() == Qt.CursorShape.ClosedHandCursor

    panel.mouseReleaseEvent(mouse_event(
        QMouseEvent.Type.MouseButtonRelease, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
    ))

    assert panel.cursor().shape() == Qt.CursorShape.OpenHandCursor


def test_release_saves_where_the_panel_was_dropped():
    panel = make_panel()
    panel.move(400, 300)
    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    panel.mouseMoveEvent(mouse_event(
        QMouseEvent.Type.MouseMove, panel, QPoint(470, 350),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
    ))

    panel.mouseReleaseEvent(mouse_event(
        QMouseEvent.Type.MouseButtonRelease, panel, QPoint(470, 350),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
    ))

    assert panel._settings.x == panel.x() == 460
    assert panel._settings.y == panel.y() == 340


def test_press_move_release_settles_the_panel():
    """The three release duties together, in the order they happen.

    One test for the whole gesture because the three of them are one piece of
    behaviour: the panel followed the cursor, then gave back the cursor, forgot
    the grab and wrote down where it landed.
    """
    panel = make_panel()
    panel.move(400, 300)

    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    panel.mouseMoveEvent(mouse_event(
        QMouseEvent.Type.MouseMove, panel, QPoint(470, 350),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
    ))
    panel.mouseReleaseEvent(mouse_event(
        QMouseEvent.Type.MouseButtonRelease, panel, QPoint(470, 350),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
    ))

    assert panel.pos() == QPoint(460, 340)
    assert panel._drag_origin is None
    assert panel.cursor().shape() == Qt.CursorShape.OpenHandCursor
    assert (panel._settings.x, panel._settings.y) == (460, 340)


def test_a_button_less_move_after_a_hide_and_re_show_does_not_drag():
    """The reported fault, end to end.

    apply_window_flags() re-shows a widget that was already visible, and Qt
    swallows the release that was in flight across the hide. _drag_origin
    survived it with the closed-hand cursor still set, so the next move -- with
    no button down at all -- dragged the panel from (400, 300) to (590, 490)
    under a cursor that was not touching it.
    """
    panel = make_panel()
    panel.move(400, 300)
    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    # hideEvent is delivered directly: Qt never sends one to a widget that was
    # never shown, and showing it is not allowed here.
    panel.hideEvent(QHideEvent())

    # The hide is where the state has to go, not the move below: a closed-hand
    # cursor and a live _drag_origin on a window that is no longer on screen is
    # the fault, and the move is only the symptom that reported it.
    assert panel._drag_origin is None, (
        "the hide left a grab armed: the next move event would be a drag"
    )
    assert panel.cursor().shape() == Qt.CursorShape.OpenHandCursor

    panel.apply_window_flags()

    panel.mouseMoveEvent(mouse_event(
        QMouseEvent.Type.MouseMove, panel, QPoint(590, 490),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
    ))

    assert panel.pos() == QPoint(400, 300), (
        f"a move with no button held dragged the panel to {panel.pos()}"
    )
    assert panel._drag_origin is None
    assert panel.cursor().shape() == Qt.CursorShape.OpenHandCursor


def test_a_move_with_no_button_held_never_drags():
    """The other half: a lost release must not even need a hide to be caught.

    SetWindowStaysOnTopHint is toggled through apply_window_flags(), which is
    not the only way a release goes missing. Whatever swallowed it, a move
    event that carries no left button is not a drag.
    """
    panel = make_panel()
    panel.move(400, 300)
    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))

    panel.mouseMoveEvent(mouse_event(
        QMouseEvent.Type.MouseMove, panel, QPoint(590, 490),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
    ))

    assert panel.pos() == QPoint(400, 300), (
        f"a move with no button held dragged the panel to {panel.pos()}"
    )
    assert panel._drag_origin is None


def test_drag_is_clamped_to_the_screen():
    panel = make_panel()
    panel.move(100, 100)
    available = panel.screen().availableGeometry()

    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(110, 110),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    panel.mouseMoveEvent(mouse_event(
        QMouseEvent.Type.MouseMove, panel, QPoint(99999, 99999),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
    ))

    # Not a pair of bounds: the panel is held against the far corner exactly,
    # with none of its canvas hanging off the screen.
    assert panel.pos() == QPoint(
        available.right() - panel.width(),
        available.bottom() - panel.height(),
    ), f"left at {panel.pos()}, dragging is what keeps it on screen"


def test_a_double_click_cancels_an_armed_drag():
    """The double click gives the drag back before it moves the panel.

    press, then a double-click with the release swallowed -- the same loss the
    hide case has -- leaves _drag_origin armed and the cursor on a closed hand.
    The buttons() guard stops the panel from actually being dragged, but the
    cursor claims "grabbing" until the next move or press, on a panel that has
    already jumped back to the corner.
    """
    panel = make_panel()
    panel.move(400, 300)
    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    assert panel.cursor().shape() == Qt.CursorShape.ClosedHandCursor

    panel.mouseDoubleClickEvent(mouse_event(
        QMouseEvent.Type.MouseButtonDblClick, panel, QPoint(420, 320),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))

    assert panel._drag_origin is None, "the double click left the grab armed"
    assert panel.cursor().shape() == Qt.CursorShape.OpenHandCursor


def test_a_double_click_drives_the_panel_back_to_the_corner():
    """The handler, through the sequence the user's hand actually produces.

    Calling restore_default_position() directly proved the corner maths and
    nothing about the wiring: mouseDoubleClickEvent's whole body could be `pass`
    and the suite stayed green. This is the press/release/double-click/release
    Qt delivers, and it is checked against both edges rather than one of them.
    """
    panel = make_panel()
    panel.move(10, 10)
    available = panel.screen().availableGeometry()
    press_at, released_at = QPoint(20, 20), QPoint(20, 20)

    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, press_at,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    panel.mouseReleaseEvent(mouse_event(
        QMouseEvent.Type.MouseButtonRelease, panel, released_at,
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
    ))
    panel.mouseDoubleClickEvent(mouse_event(
        QMouseEvent.Type.MouseButtonDblClick, panel, press_at,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    panel.mouseReleaseEvent(mouse_event(
        QMouseEvent.Type.MouseButtonRelease, panel, released_at,
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
    ))

    assert panel.pos() == QPoint(
        available.right() - panel.width() - theme.CORNER_MARGIN,
        available.top() + theme.CORNER_MARGIN,
    ), f"left at {panel.pos()} instead of the corner"


def test_a_right_button_double_click_leaves_the_panel_alone():
    """The corner restore is a left-button gesture, but the grab is not.

    mouseDoubleClickEvent used to act on event.button() not at all, so the
    right button snapped the panel back to the corner. Restricting that was
    right and it introduced a leak: the early return for other buttons went in
    front of the _cancel_drag(), so a right double click during a drag left
    _drag_origin armed and the cursor on a closed hand. Every button path ends
    the grab; only the left one moves the panel.
    """
    panel = make_panel()
    panel.move(400, 300)
    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    assert panel.cursor().shape() == Qt.CursorShape.ClosedHandCursor

    panel.mouseDoubleClickEvent(mouse_event(
        QMouseEvent.Type.MouseButtonDblClick, panel, QPoint(420, 320),
        Qt.MouseButton.RightButton, Qt.MouseButton.RightButton,
    ))

    assert panel.pos() == QPoint(400, 300), (
        f"a right-button double click moved the panel to {panel.pos()}"
    )
    assert panel._drag_origin is None, (
        "the right button did not move the panel but left the grab armed"
    )
    assert panel.cursor().shape() == Qt.CursorShape.OpenHandCursor


def test_a_middle_click_gives_the_drag_back():
    """Every button path ends the grab, not just the ones that move the panel.

    The middle click asks the app to quit, so the stuck cursor never survives
    long enough to be seen -- but it is the same state as every other path, and
    a test suite that pins it everywhere else should pin it here too.
    """
    panel = make_panel()
    panel.move(400, 300)
    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    asked = []
    panel.quit_requested.connect(lambda: asked.append(True))

    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(420, 320),
        Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton,
    ))

    assert asked == [True], "the middle click stopped asking the app to quit"
    assert panel._drag_origin is None, "the middle click left the grab armed"
    assert panel.cursor().shape() == Qt.CursorShape.OpenHandCursor


def test_middle_click_asks_the_app_to_quit():
    panel = make_panel()
    asked = []
    panel.quit_requested.connect(lambda: asked.append(True))

    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(50, 50),
        Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton,
    ))

    assert asked == [True]


def test_right_click_asks_for_the_menu_where_the_cursor_is():
    """The panel reports a global position; the app owns the menu."""
    panel = make_panel()
    seen = []
    panel.menu_requested.connect(seen.append)

    panel.contextMenuEvent(QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(20, 30), QPoint(420, 330),
    ))

    assert seen == [QPoint(420, 330)]


def test_clamp_keeps_the_panel_on_screen():
    panel = make_panel()
    available = panel.screen().availableGeometry()
    clamped = panel.clamp_to_screen(QPoint(-5000, -5000))
    assert clamped.x() >= available.left()
    assert clamped.y() >= available.top()
    clamped = panel.clamp_to_screen(QPoint(99999, 99999))
    assert clamped.x() <= available.right() - panel.width()
    assert clamped.y() <= available.bottom() - panel.height()


def test_a_position_from_a_build_with_a_smaller_panel_is_honoured():
    """No migration for the top-left left behind in settings.json.

    The stored pair is a top-left corner, and a corner that put a 280 px wide
    panel on screen still puts a 296 px one on screen: the widget grew into the
    margin it was already drawn over. Rewriting or discarding it would move the
    panel out from under the user on the first start of this build, so the
    answer is to leave the number alone and let clamping judge it against the
    size the panel is now.
    """
    panel = make_panel()
    available = panel.screen().availableGeometry()
    stored = QPoint(available.right() - theme.WIDTH - 20, available.top() + 40)

    assert panel.clamp_to_screen(stored) == stored


def test_two_ticks_through_paint_event_match_one_fresh_paint():
    """The reused painter must land on the same pixels twice, and match paint().

    paint() translates the canvas by theme.BLEED, so a painter that kept that
    translate would start every tick 8 px further right than the last and walk
    the panel off the canvas -- while the golden images, each painted by a
    fresh painter, stayed green. Task 5 proves paint() hands the painter back
    unchanged; this proves the widget uses one the way the app does, which is
    the only place that accumulation could happen.

    ts defaults to 0.0, so the header carries no age text and the wall clock
    paint() reads for it cannot move a pixel between the three renders.
    """
    panel = make_panel()
    panel.set_alpha(theme.DEFAULT_ALPHA)
    for cpu in (28.0, 34.0, 41.0):
        panel.apply_snapshot(Snapshot(cpu_pct=cpu, ram_used_gb=11.4, ram_total_gb=32.0))

    first = render_panel(panel)
    second = render_panel(panel)
    reference = fresh_paint(panel)

    assert max_channel_delta(second, first) == 0, (
        "the second tick painted different pixels than the first: the painter "
        "kept state between ticks"
    )
    assert max_channel_delta(second, reference) == 0, (
        f"two ticks through paintEvent differ from one paint() call by "
        f"{max_channel_delta(second, reference)} counts: the widget's own "
        "painting is drifting, clipped, or drawing nothing at all"
    )


def test_paint_event_ends_every_painter_it_opens(monkeypatch):
    """A painter left open stays active on the widget, and the next tick's
    QPainter(self) then finds the device already in use.

    The pixels cannot show this: PyQt's garbage collector ends an abandoned
    painter as the frame unwinds, so a missing end() still renders correctly
    once and only misbehaves later, inside Qt. recording_painters() keeps the
    references that make it visible.
    """
    with recording_painters(monkeypatch) as opened:
        panel = make_panel()
        panel.apply_snapshot(Snapshot(cpu_pct=34.0))
        render_panel(panel)

        assert opened, "paintEvent opened no painter, so this test proves nothing"
        still_active = active_painters(opened)
        assert not still_active, (
            f"paintEvent returned with painter(s) {still_active} still active: "
            "they were never ended, so the next tick paints onto a device that is "
            "already in use"
        )


@pytest.mark.parametrize(
    "error",
    [
        ValueError("not a number"),
        KeyError("cpu_pct"),
        TypeError("not subscriptable"),
        ZeroDivisionError("division by zero"),
        AttributeError("no attribute 'ts'"),
    ],
    ids=["ValueError", "KeyError", "TypeError", "ZeroDivisionError", "AttributeError"],
)
def test_the_render_guard_is_wide_enough_for_any_fault(caplog, monkeypatch, error):
    """`except Exception`, not `except RuntimeError`.

    Every other failure test injects a RuntimeError because that is the easy
    one to raise, which is exactly why a guard narrowed to RuntimeError passes
    all of them. The faults painter.py can plausibly hit -- an attribute a thin
    snapshot does not carry, a value that will not divide, a key that is not
    there -- are ValueError, TypeError, KeyError and AttributeError, and the
    panel would die on the first of them.

    Driven through paintEvent directly, which is the point of it: the property
    being pinned is that paintEvent *returns*. A guard that is narrowed, or gone,
    raises into this frame and fails here with a traceback that says what,
    instead of handing the exception to Qt, which aborts the interpreter. This
    and test_a_failed_frame_still_ends_its_painter are the two halves of the
    guard: one that the exception does not escape, one that the painter is ended
    when it does.
    """
    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))

    def raiser(*args, **kwargs):
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(overlay, "paint", raiser)
        panel.paintEvent(None)

    assert type(error).__name__ in caplog.text, (
        f"a {type(error).__name__} out of the renderer was swallowed without a "
        "word in the log"
    )


@renders_failing_frames
def test_a_failed_frame_still_ends_its_painter(monkeypatch):
    """The guard must not swallow the painter along with the exception.

    finally: painter.end() is what keeps the widget's paint device free for the
    next tick. An except that returned before the end() would leave the painter
    active on the device, and the failure would trade one broken frame for a
    broken panel.

    Through render(), not a direct paintEvent call, and the difference is the
    whole test: QPainter on a widget that was never shown never activates, so
    isActive() would read False for a painter that never began. render()
    redirects the painter onto a real device, which is what makes "ended" mean
    something here -- and is why this test, and only this kind, runs in the
    child process.
    """
    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))

    with recording_painters(monkeypatch) as opened:
        with failing_render(monkeypatch, failing_paint("simulated render failure")):
            render_panel(panel)

        assert opened, "paintEvent opened no painter, so this test proves nothing"
        still_active = active_painters(opened)
        assert not still_active, (
            f"paintEvent failed with painter(s) {still_active} still active: the "
            "render guard swallowed the painter as well as the exception"
        )


@renders_failing_frames
def test_one_failing_fault_is_logged_once_not_once_per_tick(caplog, monkeypatch):
    """update() fires every theme.TICK_MS for as long as the widget is up.

    An unguarded logger.exception() would append the same traceback to
    app_debug.log tens of thousands of times a day, which is the log growth the
    rewrite set out to stop. Five failed frames are one fault, so they are one
    log record.
    """
    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))

    with caplog.at_level(logging.ERROR, logger="widget.overlay"):
        with failing_render(monkeypatch, failing_paint("simulated render failure")):
            for _ in range(5):
                render_panel(panel)

    assert len(caplog.records) == 1, (
        f"five identical failures logged {len(caplog.records)} times: one fault "
        "is one log record"
    )


def test_a_clean_frame_no_longer_unblocks_the_log(caplog, monkeypatch):
    """What wave 2 believed, undone on purpose, with the reason on record.

    Wave 2 dropped the fault memo whenever a frame painted cleanly, so the next
    failure was written out at once whatever the clock said. That bypass is the
    unbounded case: one bad frame then one clean frame, repeating, is 21 600
    records a day -- the log growth this rewrite exists to remove. A clean frame
    now means nothing to the log. Only the floor decides, so the fault that
    follows it waits its turn, and is named the moment the floor is up.
    """
    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))
    clock = Clock()
    monkeypatch.setattr(overlay, "time", clock)

    with caplog.at_level(logging.ERROR, logger="widget.overlay"):
        with monkeypatch.context() as patch:
            patch.setattr(overlay, "paint", fault_from_probe_a)
            panel.paintEvent(None)
            assert len(caplog.records) == 1

            # A frame that paints cleanly, then a fault of a different kind --
            # the exact path the previous test covered, and the one that used to
            # be reported immediately.
            patch.undo()
            healthy = render_panel(panel)
            assert max_channel_delta(healthy, fresh_paint(panel)) == 0, (
                "the frame meant to be healthy did not paint the panel, so what "
                "follows is not measuring what it claims: the renderer was "
                "still a raiser, or the frame drew nothing"
            )

            patch.setattr(overlay, "paint", fault_from_probe_b)
            panel.paintEvent(None)
            assert len(caplog.records) == 1, (
                "a different fault inside the floor window was reported at once: "
                "the bypass the floor was meant to remove is still here"
            )

            clock.now += EXPECTED_LOG_FLOOR
            panel.paintEvent(None)

    assert len(caplog.records) == 2
    assert "probe B" in caplog.text, (
        "the record written after the interval names an older fault: a fault "
        "that replaced another one is reported as the wrong thing"
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_render_failure_logging_is_bounded_by_the_floor(caplog, monkeypatch, scenario):
    """One hour of two-second ticks, five different ways to keep failing.

    Every scenario must produce the same count: what PAINT_ERROR_LOG_INTERVAL
    allows in the hour that passed. A count that moves with the number of frames,
    or with how many different faults are in play, is the log growing again.

    The frames are driven through paintEvent(None) rather than render(): the
    logging is identical and it is the cheap way to put 1800 frames through.
    """
    frames = 1800  # one per theme.TICK_MS, for an hour
    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))
    clock = Clock()
    monkeypatch.setattr(overlay, "time", clock)

    with caplog.at_level(logging.ERROR, logger="widget.overlay"):
        with monkeypatch.context() as patch:
            for index in range(frames):
                fails, fault = fault_on_frame(scenario, index)
                if fails:
                    patch.setattr(overlay, "paint", fault)
                    panel.paintEvent(None)
                clock.tick()

    assert overlay.PAINT_ERROR_LOG_INTERVAL == EXPECTED_LOG_FLOOR
    expected = 1 + int((frames - 1) * theme.TICK_MS / 1000.0 / EXPECTED_LOG_FLOOR)
    assert len(caplog.records) == expected, (
        f"{scenario}: {frames} failed frames logged {len(caplog.records)} records, "
        f"not the {expected} that one {EXPECTED_LOG_FLOOR:.0f}s floor allows in an "
        "hour -- the log is growing with the frame count, or with the fault"
    )
    assert len(caplog.records) < frames / 100, (
        f"{scenario}: the floor is not bounding anything"
    )


def test_a_new_raise_site_is_reported_when_the_floor_expires(caplog, monkeypatch):
    """The one guarantee that did not survive, written down.

    A fault from a brand-new raise site, arriving while the floor is still
    counting down, is not reported until the floor expires. That is accepted:
    the panel is visibly broken and the record already on file names a fault
    from the same widget, so nothing is hidden -- the new site is simply the
    next thing said, at most five minutes later.
    """
    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))
    clock = Clock()
    monkeypatch.setattr(overlay, "time", clock)

    with caplog.at_level(logging.ERROR, logger="widget.overlay"):
        with monkeypatch.context() as patch:
            patch.setattr(overlay, "paint", fault_from_probe_a)
            panel.paintEvent(None)

            patch.setattr(overlay, "paint", fault_from_probe_b)
            panel.paintEvent(None)
            assert "probe B" not in caplog.text, (
                "a new raise site inside the floor window was reported at once: "
                "the floor only bounds the record count if it bounds this too"
            )
            assert len(caplog.records) == 1

            clock.now += EXPECTED_LOG_FLOOR
            panel.paintEvent(None)

    assert "probe B" in caplog.text, (
        "the new raise site was never reported at all: a fault that arrives "
        "during a window has to be said at the next one"
    )


def test_paint_event_reads_alpha_through_the_accessor():
    """One source of truth for alpha, read the same way from both sides.

    set_alpha() writes _settings.alpha and current_alpha() reads it, so reading
    the field directly inside paintEvent gives one value two paths to itself --
    and the paths can only start disagreeing once something else moves alpha
    without going through set_alpha().

    The spy is the point: both readings return the same number, so comparing
    them would pass whatever paintEvent did. This fails the day someone reaches
    past the accessor.
    """
    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))

    asked = []
    accessor = panel.current_alpha

    def spy():
        asked.append(None)
        return accessor()

    panel.current_alpha = spy
    render_panel(panel)

    assert asked, "paintEvent read _settings.alpha directly instead of the accessor"


def test_installing_a_failing_renderer_needs_registration(monkeypatch):
    """The mechanism itself, checked from the outside.

    This test is deliberately *not* registered, and it is what proves the guard
    is real: without it, adding an abort-prone test and forgetting the decorator
    put the abort straight back into the parent run with nothing to warn about
    it, twice. A guard that only existed as a comment could not fail here.
    """
    with pytest.raises(RuntimeError, match="renders_failing_frames"):
        with failing_render(monkeypatch, failing_paint("never installed")):
            pass


def test_installing_a_failing_renderer_is_refused_by_render_panel(monkeypatch):
    """...and so does the door itself, in case the patch is made by hand.

    render_panel() is the only thing in this file that calls widget.render(), so
    it is where an unregistered failing renderer gets caught. The exception is
    raised before a single frame is drawn, which is the whole point: a failure
    here costs one test, where the abort costs every result after it.
    """
    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))
    monkeypatch.setattr(overlay, "paint", failing_paint("never installed"))

    with pytest.raises(RuntimeError, match="renders_failing_frames"):
        render_panel(panel)


def test_a_registered_test_may_render_failing_frames(monkeypatch):
    """The guard must not be a blanket ban, or nobody would use it.

    A registered test draws a failing frame and gets a normal failure out of it,
    which is what the two registered tests above rely on.
    """
    assert "test_a_failed_frame_still_ends_its_painter" in ABORT_PRONE_TESTS
    assert "test_one_failing_fault_is_logged_once_not_once_per_tick" in ABORT_PRONE_TESTS
    assert set(ABORT_PRONE_TESTS) <= set(globals()), (
        f"ABORT_PRONE_TESTS names something that is not a test in this file: "
        f"{ABORT_PRONE_TESTS}"
    )


def test_a_painter_that_cannot_be_created_does_not_take_the_process(caplog, monkeypatch):
    """QPainter(self) is inside the guard, not in front of it.

    The documented contract is that a frame which fails costs a frame, and the
    painter construction is the one statement in paintEvent that could raise
    before the guarded call -- a widget whose paint device cannot give a painter
    out. Driven through paintEvent directly, so this stays out of the child
    process: it never renders.
    """
    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))

    def refusing_painter(*args, **kwargs):
        raise RuntimeError("no paint device available")

    monkeypatch.setattr(overlay, "QPainter", refusing_painter)
    panel.paintEvent(None)

    assert "no paint device available" in caplog.text


def test_a_cancelled_drag_still_remembers_where_the_panel_landed():
    """Ending a drag writes down where the panel ended up, however it ended.

    The panel has already moved by the time any of these paths run: the cancel
    is about the grab, not the position. Leaving the position to closeEvent
    meant that dragging the panel somewhere and then hiding it lost the drop --
    settings.json still held the old corner, and the panel came back there on
    the next start.
    """
    panel = make_panel()
    panel.move(400, 300)
    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    panel.mouseMoveEvent(mouse_event(
        QMouseEvent.Type.MouseMove, panel, QPoint(470, 350),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
    ))
    assert panel.pos() == QPoint(460, 340)

    panel.hideEvent(QHideEvent())

    assert panel._settings.x == 460
    assert panel._settings.y == 340, (
        f"the panel was dropped at {panel.pos()} but the settings still say "
        f"{panel._settings.x}, {panel._settings.y}"
    )


def test_a_button_less_move_remembers_where_the_panel_landed():
    """Same, for the other way a drag is cancelled: the cursor stops dragging."""
    panel = make_panel()
    panel.move(400, 300)
    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    panel.mouseMoveEvent(mouse_event(
        QMouseEvent.Type.MouseMove, panel, QPoint(470, 350),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
    ))
    panel.mouseReleaseEvent(mouse_event(
        QMouseEvent.Type.MouseButtonRelease, panel, QPoint(470, 350),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
    ))
    panel.mouseMoveEvent(mouse_event(
        QMouseEvent.Type.MouseMove, panel, QPoint(9999, 9999),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
    ))

    assert panel._settings.x == 460
    assert panel._settings.y == 340


def test_a_button_less_move_is_accepted():
    """Every other branch of this class accepts the event it handled."""
    panel = make_panel()
    panel.move(400, 300)
    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    stray = mouse_event(
        QMouseEvent.Type.MouseMove, panel, QPoint(420, 320),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
    )
    # A fresh QMouseEvent arrives accepted, so the assertion would be vacuous.
    stray.ignore()

    panel.mouseMoveEvent(stray)

    assert stray.isAccepted(), (
        "the move that cancelled the drag was left unhandled: it fell through "
        "to QWidget, which ignores it"
    )


def test_a_failed_frame_does_not_take_the_process_with_it():
    """A failing render costs a frame, not the process -- proven out of process.

    PyQt calls qFatal() when a Python exception escapes a reimplemented virtual
    method, so a raise out of paintEvent ends the interpreter, and this widget is
    meant to sit on someone's screen all day. painter.py reads cpu_live_mhz,
    cpu_nominal_mhz and the two net columns straight off the dataclass rather
    than through an accessor, so any snapshot thinner than metrics.Snapshot would
    take the whole application down from inside a paint event.

    Asserting that here is impossible: the test that proves it has to drive a
    failing render(), and if the guard is gone that test does not fail, it
    aborts -- taking the run with it. Ordering does not help, because the first
    such test in the file is the one that dies. So the tests decorated
    @renders_failing_frames run in a child process and this asserts on its exit
    code, which means a regression costs one failure with the child's output
    attached instead of every result after it, the painter goldens included.

    The list comes from the decorators, so it cannot be out of date; what is
    checked here is the other half -- that each of those tests really does run,
    really does pass, in the child.
    """
    if os.environ.get(ABORT_CANARY_CHILD) == "1":
        pytest.skip("this is the child run; the parent asserts on its exit code")

    assert ABORT_PRONE_TESTS, (
        "no test is registered as rendering failing frames, so the canary has "
        "nothing to contain: either the registration decorator stopped being "
        "used, or the render guard's coverage is no longer being exercised"
    )
    child = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "-k", " or ".join(ABORT_PRONE_TESTS)],
        cwd=str(REPO_ROOT),
        env={**os.environ, ABORT_CANARY_CHILD: "1"},
        capture_output=True,
        text=True,
        timeout=300,
    )
    # A PyQt abort writes no summary, so its own output is usually empty; a
    # child that died collecting, or on an assertion, puts the reason on stderr.
    report = f"stdout:\n{child.stdout[-2000:]}\nstderr:\n{child.stderr[-2000:]}"

    # Anti-vacuity first: a child that selected nothing, or that skipped them,
    # would exit 0 and this test would pass without ever driving a failing
    # render -- which is the thing it exists to prove.
    for name in ABORT_PRONE_TESTS:
        assert f"{name} PASSED" in child.stdout, (
            f"{name} did not run and pass in the child process, so the abort it "
            f"is here to contain would still land in this run:\n{report}"
        )
    assert child.returncode == 0, (
        f"the child process that drives failing renders exited {child.returncode} "
        f"rather than 0. On Windows a negative code is the abort PyQt raises for "
        f"an exception escaping paintEvent, so the render guard is gone -- and the "
        f"render-driven tests in this file would otherwise have taken this run "
        f"with them:\n{report}"
    )
