"""Footprint movement mode GUI tests."""

# Qt uses compiled extension modules; tests intentionally inspect GUI state.
# pylint: disable=wrong-import-position,no-name-in-module,redefined-outer-name
# pylint: disable=protected-access,unused-argument

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tnasrevner.gui import MainWindow
from tnasrevner.project import (
    Device,
    ImageAsset,
    Pad,
    ProjectDocument,
    ProjectStore,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    """Provide one headless Qt application for GUI tests."""
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app: QApplication, tmp_path: Path) -> MainWindow:
    """Create an isolated main window."""
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    result = MainWindow(show_startup=False, settings=settings)
    yield result
    result._dirty = False
    result.close()


def _movement_project() -> ProjectDocument:
    """Return one THT footprint with separately persisted face coordinates."""
    device = Device(
        "J1",
        "top",
        0.2,
        0.3,
        "Connector_Generic",
        "DIP-2",
        "assets/kicad/j1.kicad_mod",
        "a" * 40,
        device_id="device",
    )
    return ProjectDocument(
        "Project",
        "Board",
        devices=[device],
        pads=[
            Pad(
                "J1.1",
                "top",
                0.1,
                0.2,
                "top-1",
                0.05,
                0.05,
                device_id="device",
                number="1",
            ),
            Pad(
                "J1.1.bottom",
                "bottom",
                0.84,
                0.22,
                "bottom-1",
                0.05,
                0.05,
                device_id="device",
                number="1",
            ),
            Pad(
                "J1.2",
                "top",
                0.3,
                0.2,
                "top-2",
                0.05,
                0.05,
                device_id="device",
                number="2",
            ),
            Pad(
                "J1.2.bottom",
                "bottom",
                0.64,
                0.22,
                "bottom-2",
                0.05,
                0.05,
                device_id="device",
                number="2",
            ),
        ],
    )


def test_move_palette_cycles_face_scope_and_escape_exits(
    window: MainWindow, tmp_path: Path
) -> None:
    """The palette cycles Dual/Single and Escape leaves move mode."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")

    window._move_button.click()

    assert window._move_mode == "dual"
    assert window._move_button.isChecked()
    assert window._move_button.text() == "↔️"
    assert window.statusBar().currentMessage() == "Dual face move"

    window._move_button.click()

    assert window._move_mode == "single"
    assert window._move_button.isChecked()
    assert window.statusBar().currentMessage() == "Single face move"

    QTest.keyClick(window, Qt.Key.Key_Escape)

    assert window._move_mode is None
    assert not window._move_button.isChecked()
    assert window.statusBar().currentMessage() == ""


def test_dual_face_move_moves_whole_footprint_with_mirrored_x(
    window: MainWindow, tmp_path: Path
) -> None:
    """Dual movement shifts every device pad and mirrors horizontal movement."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = _movement_project()
    original = {pad.pad_id: pad for pad in window.project.pads}
    window._reset_history()
    window._set_move_mode("dual")

    window._start_pad_move("top", "top-1", 0.125, 0.225)
    window._finish_pad_move("top", "top-1", 0.225, 0.275)

    moved = {pad.pad_id: pad for pad in window.project.pads}
    for pad_id in ("top-1", "top-2"):
        assert moved[pad_id].x == pytest.approx(original[pad_id].x + 0.1)
        assert moved[pad_id].y == pytest.approx(original[pad_id].y + 0.05)
    for pad_id in ("bottom-1", "bottom-2"):
        assert moved[pad_id].x == pytest.approx(original[pad_id].x - 0.1)
        assert moved[pad_id].y == pytest.approx(original[pad_id].y + 0.05)
    assert window.project.devices[0].x == pytest.approx(0.3)
    assert window.project.devices[0].y == pytest.approx(0.35)
    assert window._dirty

    window.undo()

    restored = {pad.pad_id: pad for pad in window.project.pads}
    assert restored == original
    assert window.project.devices[0].x == pytest.approx(0.2)
    assert window.project.devices[0].y == pytest.approx(0.3)


def test_single_face_move_preserves_independent_face_offset(
    window: MainWindow, tmp_path: Path
) -> None:
    """Single movement changes only the selected face and survives serialization."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = _movement_project()
    original = {pad.pad_id: pad for pad in window.project.pads}
    original_device = window.project.devices[0]
    window._set_move_mode("single")

    window._start_pad_move("bottom", "bottom-1", 0.865, 0.245)
    window._finish_pad_move("bottom", "bottom-1", 0.915, 0.295)

    moved = {pad.pad_id: pad for pad in window.project.pads}
    for pad_id in ("bottom-1", "bottom-2"):
        assert moved[pad_id].x == pytest.approx(original[pad_id].x + 0.05)
        assert moved[pad_id].y == pytest.approx(original[pad_id].y + 0.05)
    for pad_id in ("top-1", "top-2"):
        assert moved[pad_id] == original[pad_id]
    assert window.project.devices[0] == original_device

    restored = ProjectDocument.from_dict(window.project.to_dict())
    restored_pads = {pad.pad_id: pad for pad in restored.pads}
    assert restored_pads["bottom-1"].x == pytest.approx(moved["bottom-1"].x)
    assert restored_pads["bottom-1"].y == pytest.approx(moved["bottom-1"].y)


def test_shift_click_then_pointer_click_moves_pad(
    window: MainWindow, tmp_path: Path
) -> None:
    """A Shift-click selects the move handle and the next click places it."""
    window.store = ProjectStore(tmp_path / "board.revp")
    image = QImage(1000, 1000, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.project = ProjectDocument(
        "Project",
        "Board",
        images=[ImageAsset("top", "assets/top.png", "top.png")],
        pads=[Pad("P1", "top", 0.2, 0.2, "pad", 0.1, 0.1)],
    )
    window._refresh_views()
    window.show()
    QApplication.processEvents()
    view = window._views["top"]
    window._set_move_mode("dual")
    view.apply_view_state((2.5, 0.7, 0.3))
    QApplication.processEvents()
    viewport_before = view.view_state()
    start = QPoint(
        round(0.25 * (view._label.width() - 1)),
        round(0.25 * (view._label.height() - 1)),
    )
    target = QPoint(
        round(0.45 * (view._label.width() - 1)),
        round(0.40 * (view._label.height() - 1)),
    )

    QTest.mouseClick(
        view._label,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ShiftModifier,
        start,
    )
    QTest.mouseMove(view._label, target)
    QTest.mouseClick(view._label, Qt.MouseButton.LeftButton, pos=target)
    QApplication.processEvents()

    pad = window.project.pads[0]
    assert pad.x == pytest.approx(0.4, abs=0.01)
    assert pad.y == pytest.approx(0.35, abs=0.01)
    assert window._moving_pad_context is None
    assert window._move_mode == "dual"
    assert window.statusBar().currentMessage() == "Dual face move"
    assert view.view_state() == pytest.approx(viewport_before, abs=0.01)
