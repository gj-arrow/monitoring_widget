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


def test_double_click_restores_the_corner():
    panel = make_panel()
    panel.move(10, 10)
    panel.restore_default_position()
    available = panel.screen().availableGeometry()
    assert panel.x() > available.left()


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

    reference = blank_canvas()
    reference_painter = QPainter(reference)
    try:
        paint(
            reference_painter,
            panel._snapshot,
            panel._histories,
            panel.current_alpha(),
        )
    finally:
        reference_painter.end()

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


def test_a_failed_frame_does_not_take_the_process_with_it(caplog, monkeypatch):
    """One bad frame must cost a frame, not the desktop.

    PyQt calls qFatal() when a Python exception escapes a reimplemented virtual
    method, so a raise out of paintEvent ends the process -- and this widget is
    meant to sit on someone's screen all day. painter.py reads snapshot.ts,
    cpu_mhz and cpu_max_mhz straight off the dataclass rather than through an
    accessor, so any snapshot thinner than metrics.Snapshot aborts the whole
    application without this guard.

    Rendered inside pytest.raises: outside it, a raise from paintEvent does not
    come back as a Python exception at all, it takes the interpreter down. That
    is the whole point of the guard, and the only way a test can hold the
    un-guarded implementation still enough to observe it.
    """
    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))

    def raising_paint(*args, **kwargs):
        raise RuntimeError("simulated render failure")

    with monkeypatch.context() as patch:
        patch.setattr(overlay, "paint", raising_paint)
        for _ in range(5):
            image = render_panel(panel)
            assert not image.isNull(), "the panel stopped painting anything at all"

    assert "simulated render failure" in caplog.text, (
        "a frame that failed to render said nothing: the failure would only be "
        "visible as a panel that quietly stopped updating"
    )


def test_a_failed_frame_still_ends_its_painter(caplog, monkeypatch):
    """The guard must not swallow the painter along with the exception.

    finally: painter.end() is what keeps the widget's paint device free for the
    next tick. An except that returns before the end() would leave the painter
    active on the device, and the failure would trade one broken frame for a
    broken panel.
    """
    def raising_paint(*args, **kwargs):
        raise RuntimeError("simulated render failure")

    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))

    with recording_painters(monkeypatch) as opened:
        with monkeypatch.context() as patch:
            patch.setattr(overlay, "paint", raising_paint)
            try:
                render_panel(panel)
            except RuntimeError:
                # Without the guard this raise escapes paintEvent, which ends
                # the interpreter. Swallowed here so the assertion below can
                # still report on the painter instead of taking the run with it.
                pass

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
    log record; a different fault is logged at once, because that one is new
    information.
    """
    def raising_paint(*args, **kwargs):
        raise RuntimeError("simulated render failure")

    def other_paint(*args, **kwargs):
        raise ValueError("a different fault entirely")

    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))

    with caplog.at_level(logging.ERROR, logger="widget.overlay"):
        with monkeypatch.context() as patch:
            patch.setattr(overlay, "paint", raising_paint)
            for _ in range(5):
                render_panel(panel)
            assert len(caplog.records) == 1, (
                f"five identical failures logged {len(caplog.records)} times: "
                "one fault is one log record"
            )

            patch.setattr(overlay, "paint", other_paint)
            render_panel(panel)
            assert len(caplog.records) == 2, (
                f"a different fault logged nothing new: {len(caplog.records)} "
                "records after two distinct faults"
            )


def test_a_recovered_panel_reports_the_same_fault_again(caplog, monkeypatch):
    """A frame that paints cleanly clears the memo.

    Without this, a fault that arrives, clears and comes back an hour later --
    the common case for a probe that fails once in a while -- would be silently
    swallowed for the rest of the session, which is the log-growth fix taken too
    far.
    """
    def raising_paint(*args, **kwargs):
        raise RuntimeError("simulated render failure")

    panel = make_panel()
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))

    with caplog.at_level(logging.ERROR, logger="widget.overlay"):
        with monkeypatch.context() as patch:
            patch.setattr(overlay, "paint", raising_paint)
            render_panel(panel)
            render_panel(panel)
            assert len(caplog.records) == 1

            patch.undo()
            render_panel(panel)

            patch.setattr(overlay, "paint", raising_paint)
            render_panel(panel)

    assert len(caplog.records) == 2, (
        f"the same fault after a healthy frame logged {len(caplog.records) - 1} "
        "times: the panel never noticed it had recovered"
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
