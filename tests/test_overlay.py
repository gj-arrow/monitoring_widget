"""Panel window tests: placement, dragging, input, and the paint delegation.

Nothing here calls show(). A test run must leave no window on screen. Every
thing the panel needs works while it is hidden: render() drives paintEvent
without a native window, and QWidget.screen() falls back to the primary screen
when the widget has no window handle yet.

Where the panel needs its own painting inspected, the tests go through
panel.render() rather than repaint(): render() redirects the painter that
paintEvent creates into a QImage the test owns, so the pixels under test are
the widget's own.
"""

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import (
    QColor,
    QContextMenuEvent,
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


def make_panel(qapp):
    """A panel that has never been on screen."""
    return MonitorPanel(Settings())


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


def test_the_panel_is_the_canvas_size_and_not_the_panel_size(qapp):
    """The widget carries the panel *and* the margin its shadow bleeds into.

    painter.paint() translates by theme.BLEED and draws the drop shadow outside
    panel_rect(), so a widget sized to theme.WIDTH x theme.HEIGHT clips the
    halo at its own edge and the panel reads as a hard wall -- the defect the
    golden images had before the canvas grew. The equality is on the canvas, so
    a regression to the panel size fails here rather than looking like a
    slightly tighter shadow nobody notices.
    """
    panel = make_panel(qapp)
    assert panel.width() == theme.CANVAS_W == theme.WIDTH + 2 * theme.BLEED
    assert panel.height() == theme.CANVAS_H == theme.HEIGHT + 2 * theme.BLEED


def test_panel_does_not_steal_focus(qapp):
    panel = make_panel(qapp)
    assert panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


def test_snapshot_feeds_the_histories(qapp):
    panel = make_panel(qapp)
    for pct in (10.0, 50.0, 90.0):
        panel.apply_snapshot(Snapshot(cpu_pct=pct, ram_used_gb=1.0, ram_total_gb=2.0))
    assert len(panel._histories["cpu_pct"]) == 3
    assert panel._histories["cpu_pct"].values()[-1] == 0.9


def test_unmeasured_rows_do_not_extend_the_history(qapp):
    panel = make_panel(qapp)
    panel.apply_snapshot(Snapshot(cpu_pct=10.0))
    panel.apply_snapshot(Snapshot(cpu_pct=None))
    assert len(panel._histories["cpu_pct"]) == 1


def test_alpha_is_clamped(qapp):
    panel = make_panel(qapp)
    panel.set_alpha(5.0)
    assert panel.current_alpha() == theme.MAX_ALPHA
    panel.set_alpha(-2.0)
    assert panel.current_alpha() == theme.MIN_ALPHA


def test_the_wheel_steps_the_alpha_within_its_range(qapp):
    panel = make_panel(qapp)
    panel.set_alpha(theme.DEFAULT_ALPHA)

    panel.wheelEvent(wheel(120))
    assert panel.current_alpha() == pytest.approx(theme.DEFAULT_ALPHA + 0.05)

    for _ in range(20):
        panel.wheelEvent(wheel(120))
    assert panel.current_alpha() == theme.MAX_ALPHA

    panel.wheelEvent(wheel(-120))
    assert panel.current_alpha() < theme.MAX_ALPHA


def test_drag_moves_the_panel(qapp):
    panel = make_panel(qapp)
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


def test_drag_is_clamped_to_the_screen(qapp):
    panel = make_panel(qapp)
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


def test_double_click_restores_the_corner(qapp):
    panel = make_panel(qapp)
    panel.move(10, 10)
    panel.restore_default_position()
    available = panel.screen().availableGeometry()
    assert panel.x() > available.left()


def test_middle_click_asks_the_app_to_quit(qapp):
    panel = make_panel(qapp)
    asked = []
    panel.quit_requested.connect(lambda: asked.append(True))

    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(50, 50),
        Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton,
    ))

    assert asked == [True]


def test_right_click_asks_for_the_menu_where_the_cursor_is(qapp):
    """The panel reports a global position; the app owns the menu."""
    panel = make_panel(qapp)
    seen = []
    panel.menu_requested.connect(seen.append)

    panel.contextMenuEvent(QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(20, 30), QPoint(420, 330),
    ))

    assert seen == [QPoint(420, 330)]


def test_clamp_keeps_the_panel_on_screen(qapp):
    panel = make_panel(qapp)
    available = panel.screen().availableGeometry()
    clamped = panel.clamp_to_screen(QPoint(-5000, -5000))
    assert clamped.x() >= available.left()
    assert clamped.y() >= available.top()
    clamped = panel.clamp_to_screen(QPoint(99999, 99999))
    assert clamped.x() <= available.right() - panel.width()
    assert clamped.y() <= available.bottom() - panel.height()


def test_a_position_from_a_build_with_a_smaller_panel_is_honoured(qapp):
    """No migration for the top-left left behind in settings.json.

    The stored pair is a top-left corner, and a corner that put a 280 px wide
    panel on screen still puts a 296 px one on screen: the widget grew into the
    margin it was already drawn over. Rewriting or discarding it would move the
    panel out from under the user on the first start of this build, so the
    answer is to leave the number alone and let clamping judge it against the
    size the panel is now.
    """
    panel = make_panel(qapp)
    available = panel.screen().availableGeometry()
    stored = QPoint(available.right() - theme.WIDTH - 20, available.top() + 40)

    assert panel.clamp_to_screen(stored) == stored


def test_two_ticks_through_paint_event_match_one_fresh_paint(qapp):
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
    panel = make_panel(qapp)
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


def test_paint_event_ends_every_painter_it_opens(qapp, monkeypatch):
    """A painter left open stays active on the widget, and the next tick's
    QPainter(self) then finds the device already in use.

    The pixels cannot show this: PyQt's garbage collector ends an abandoned
    painter as the frame unwinds, so a missing end() still renders correctly
    once and only misbehaves later, inside Qt. Holding a reference to each
    painter is what makes it visible. overlay imports QPainter by name, so
    patching the module attribute is enough to see the ones it opens.
    """
    opened = []

    class RecordingPainter(QPainter):
        def __init__(self, device):
            super().__init__(device)
            opened.append(self)

    monkeypatch.setattr(overlay, "QPainter", RecordingPainter)

    panel = make_panel(qapp)
    panel.apply_snapshot(Snapshot(cpu_pct=34.0))
    render_panel(panel)

    assert opened, "paintEvent opened no painter, so this test proves nothing"
    still_active = [index for index, p in enumerate(opened) if p.isActive()]
    assert not still_active, (
        f"paintEvent returned with painter(s) {still_active} still active: "
        "they were never ended, so the next tick paints onto a device that is "
        "already in use"
    )