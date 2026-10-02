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

import logging
from contextlib import contextmanager

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
    """Paint the widget into an image, hidden, without repainting the screen."""
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
        available.right() - panel.width() - overlay.CORNER_MARGIN,
        available.top() + overlay.CORNER_MARGIN,
    ), f"left at {panel.pos()} instead of the corner"


def test_a_right_button_double_click_leaves_the_panel_alone():
    """The corner restore is a left-button gesture.

    mouseDoubleClickEvent used to act on event.button() not at all, so the
    right button snapping the panel back to the corner was one stray right
    double-click away -- while the right button is also the one that asks for
    the tray menu.
    """
    panel = make_panel()
    panel.move(400, 300)

    panel.mouseDoubleClickEvent(mouse_event(
        QMouseEvent.Type.MouseButtonDblClick, panel, QPoint(420, 320),
        Qt.MouseButton.RightButton, Qt.MouseButton.RightButton,
    ))

    assert panel.pos() == QPoint(400, 300), (
        f"a right-button double click moved the panel to {panel.pos()}"
    )


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


def test_a_failed_frame_does_not_raise_out_of_the_handler(monkeypatch):
    """The same fault, caught where a test can see it.

    render() hands the exception to Qt, which aborts: a regression there kills
    the whole run, and a dead run tells you nothing about which test died.
    paintEvent called directly raises into this frame instead, so narrowing or
    dropping the guard fails this one test with a traceback that says what.
    """
    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))

    with monkeypatch.context() as patch:
        patch.setattr(overlay, "paint", failing_paint("simulated render failure"))
        panel.paintEvent(None)


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

    Driven through paintEvent directly so a narrowed guard fails this test
    rather than aborting the run on the way to it.
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
    something here.
    """
    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))

    with recording_painters(monkeypatch) as opened:
        with monkeypatch.context() as patch:
            patch.setattr(overlay, "paint", failing_paint("simulated render failure"))
            render_panel(panel)

        assert opened, "paintEvent opened no painter, so this test proves nothing"
        still_active = active_painters(opened)
        assert not still_active, (
            f"paintEvent failed with painter(s) {still_active} still active: the "
            "render guard swallowed the painter as well as the exception"
        )


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
        with monkeypatch.context() as patch:
            patch.setattr(overlay, "paint", failing_paint("simulated render failure"))
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
            render_panel(panel)
            assert len(caplog.records) == 1, (
                "the healthy frame was never logged, so this is not testing "
                "anything"
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


def test_a_failed_frame_does_not_take_the_process_with_it(caplog, monkeypatch):
    """The canary, kept deliberately and last in the file.

    PyQt calls qFatal() when a Python exception escapes a reimplemented virtual
    method, so a raise out of paintEvent ends the process -- and this widget is
    meant to sit on someone's screen all day. painter.py reads snapshot.ts,
    cpu_mhz and cpu_max_mhz straight off the dataclass rather than through an
    accessor, so any snapshot thinner than metrics.Snapshot would take the whole
    application down from inside a paint event.

    Driven through render() because that is the path a real frame takes, and the
    raise never comes back as a Python exception there -- it ends the
    interpreter. That is what makes this one a canary rather than an ordinary
    test: if the guard ever goes, this test cannot report a failure, it takes
    the run with it. So it sits at the end of the file, after every test that
    *can* report one has reported, rather than where it would take the rest of
    the file's results with it.
    """
    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))

    with monkeypatch.context() as patch:
        patch.setattr(overlay, "paint", failing_paint("simulated render failure"))
        for _ in range(5):
            render_panel(panel)

    assert "simulated render failure" in caplog.text, (
        "a frame that failed to render said nothing: the failure would only be "
        "visible as a panel that quietly stopped updating"
    )

    # And the guard cost the widget nothing: it paints the whole panel again
    # the moment the renderer is well.
    assert max_channel_delta(render_panel(panel), fresh_paint(panel)) == 0, (
        "the panel did not go back to painting itself once the renderer "
        "recovered: the failed frames left it damaged"
    )
