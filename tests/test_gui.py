"""Functional tests for minimal project lifecycle GUI actions."""

# Qt uses compiled extension modules; test fixtures intentionally inspect GUI
# state to verify lifecycle behavior.
# pylint: disable=wrong-import-position,no-name-in-module,redefined-outer-name
# pylint: disable=unused-argument,protected-access

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QMessageBox,
    QPushButton,
)

from tnasrevner.gui import ImageEditDialog, ImageView, MainWindow
from tnasrevner.project import (
    ImageAsset,
    Pad,
    ProjectDocument,
    ProjectFormatError,
    ProjectStore,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    """Provide one headless Qt application for GUI tests."""
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app: QApplication) -> MainWindow:
    """Create isolated main window."""
    result = MainWindow(show_startup=False)
    yield result
    result._dirty = False
    result.close()


def test_create_save_close_reopen_project(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise project creation, archive save, close, and reopen."""
    archive = tmp_path / "board.revp"

    class FakeDialog:  # pylint: disable=too-few-public-methods
        """Replacement project dialog for non-interactive testing."""

        project_name = SimpleNamespace(text=lambda: "Project")
        board_name = SimpleNamespace(text=lambda: "Board")

        def __init__(self, _parent) -> None:
            pass

        def exec(self) -> QDialog.DialogCode:
            """Pretend user accepted project details."""
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr("tnasrevner.gui.ProjectDetailsDialog", FakeDialog)
    monkeypatch.setattr(
        "tnasrevner.gui.QFileDialog.getSaveFileName",
        lambda *_args: (str(archive), ""),
    )
    monkeypatch.setattr(
        "tnasrevner.gui.QFileDialog.getOpenFileName",
        lambda *_args: (str(archive), ""),
    )

    window.new_project()
    assert window.project is not None
    assert window.save_project()
    assert archive.is_file()
    window.close_project()
    assert window.project is None

    window.open_project()
    assert window.project is not None
    assert window.project.project_name == "Project"
    assert window.project.board_name == "Board"


def test_close_project_cancel_preserves_dirty_project(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel in save guard must keep project and unsaved state."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    window._dirty = True
    monkeypatch.setattr(
        "tnasrevner.gui.QMessageBox.warning",
        lambda *_args: QMessageBox.StandardButton.Cancel,
    )

    window.close_project()

    assert window.project is not None
    assert window._dirty


def test_open_invalid_archive_reports_error(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid archive must leave current project unchanged and report failure."""
    invalid = tmp_path / "invalid.revp"
    invalid.write_bytes(b"not a zip")
    errors: list[str] = []
    monkeypatch.setattr(
        "tnasrevner.gui.QFileDialog.getOpenFileName",
        lambda *_args: (str(invalid), ""),
    )
    monkeypatch.setattr(
        "tnasrevner.gui.QMessageBox.critical",
        lambda _parent, _title, message: errors.append(message),
    )

    window.open_project()

    assert window.project is None
    assert errors and "cannot read project archive" in errors[0]


def test_image_view_displays_and_supports_zoom_pan(app: QApplication) -> None:
    """Image workspace view renders pixels and exposes zoom/pan state."""
    view = ImageView("No image")
    image = QImage(1200, 800, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    view.set_pixmap(QPixmap.fromImage(image))
    view.resize(320, 240)
    view.show()
    app.processEvents()

    view.actual_size()
    view.horizontalScrollBar().setValue(view.horizontalScrollBar().maximum())
    view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
    state = view.view_state()

    assert not view._label.pixmap().isNull()
    assert state[0] > 1.0
    assert state[1] == 1.0
    assert state[2] == 1.0
    view.close()


def test_save_persists_display_mode_zoom_and_pan(
    window: MainWindow, tmp_path: Path
) -> None:
    """Workspace display state is written to the project metadata."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    window._tabs.setCurrentIndex(0)
    window._views["top"]._scale = 2.5
    window._views["top"]._render()

    assert window.save_project()

    assert window.project.display.mode == "top"
    assert window.project.display.zoom == 2.5


def test_create_pad_from_tools_places_and_persists_marker(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tools pad action places a marker on the selected image."""
    archive = tmp_path / "board.revp"
    window.store = ProjectStore(archive)
    window.project = ProjectDocument("Project", "Board")
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.project.images.append(ImageAsset("top", "assets/top.png", "top.png"))
    window._views["top"].set_pixmap(QPixmap.fromImage(image))
    monkeypatch.setattr(window, "_choose_image_side", lambda: "top")

    window.create_pad()
    window._views["top"].pad_selected.emit(0.25, 0.75, 0.2, 0.1)

    assert len(window.project.pads) == 1
    assert window.project.pads[0].name == "P1"
    assert window.project.pads[0].x == 0.25
    assert window.project.pads[0].y == 0.75
    assert window.project.pads[0].width == 0.2
    assert window.project.pads[0].height == 0.1
    assert any(
        action.text() == "Create pad"
        for action in window._tools_dock.widget().findChildren(QPushButton)
    )

    window.create_pad()
    window._views["top"].pad_selected.emit(0.6, 0.2, 0.15, 0.15)

    assert [pad.name for pad in window.project.pads] == ["P1", "P2"]


def test_clicking_pad_toggles_links_without_resetting_zoom(
    window: MainWindow, tmp_path: Path
) -> None:
    """Pad selection preserves zoom and a second click hides connections."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad("P1", "top", 0.1, 0.1, "one", 0.1, 0.1, "GND"),
            Pad("P2", "top", 0.5, 0.5, "two", 0.1, 0.1, "GND"),
        ],
    )
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.project.images.append(ImageAsset("top", "assets/top.png", "top.png"))
    window._refresh_views()
    window._views["top"]._scale = 3.0
    window._views["top"]._render()

    window._select_pad("top", 0.15, 0.15)
    assert window._selected_pad_id == "one"
    assert window._views["top"]._scale == 3.0

    window._select_pad("top", 0.15, 0.15)
    assert window._selected_pad_id is None
    assert window._views["top"]._scale == 3.0

    window._set_pads_visible(False)
    QApplication.processEvents()
    assert not window._pads_visible
    assert window._views["top"]._scale == 3.0


def test_net_view_and_right_click_assignment(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Net assignment keeps pad name and appears in the Nets table."""
    window.project = ProjectDocument(
        "Project", "Board", pads=[Pad("P1", "top", 0.1, 0.1, "one", 0.1, 0.1)]
    )
    monkeypatch.setattr(
        "tnasrevner.gui.QInputDialog.getText", lambda *_args, **_kwargs: ("GND", True)
    )

    window._connect_pad_to_net("top", 0.15, 0.15)
    QApplication.processEvents()
    window._refresh_net_table()

    assert window.project.pads[0].name == "P1"
    assert window.project.pads[0].net == "GND"
    assert window._net_table.item(0, 0).text() == "P1"
    assert window._net_table.item(0, 1).text() == "GND"


def test_pad_mouse_rectangle_releases_placement_mode(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real drag releases placement so the next pad can be started."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.project.images.append(ImageAsset("top", "assets/top.png", "top.png"))
    window._views["top"].set_pixmap(QPixmap.fromImage(image))
    window._views["top"].show()
    monkeypatch.setattr(window, "_choose_image_side", lambda: "top")

    window.create_pad()
    QTest.mousePress(
        window._views["top"]._label, Qt.MouseButton.LeftButton, pos=QPoint(10, 10)
    )
    QTest.mouseMove(window._views["top"]._label, QPoint(40, 40))
    QTest.mouseRelease(
        window._views["top"]._label, Qt.MouseButton.LeftButton, pos=QPoint(40, 40)
    )

    assert len(window.project.pads) == 1
    assert not window._views["top"]._pad_placement

    window.create_pad()
    QTest.mousePress(
        window._views["top"]._label,
        Qt.MouseButton.LeftButton,
        pos=QPoint(50, 50),
    )
    QTest.mouseMove(window._views["top"]._label, QPoint(80, 80))
    QTest.mouseRelease(
        window._views["top"]._label,
        Qt.MouseButton.LeftButton,
        pos=QPoint(80, 80),
    )

    assert [pad.name for pad in window.project.pads] == ["P1", "P2"]


def test_import_image_stores_selected_side(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One import action stores image after side selection."""
    source = tmp_path / "board-photo.png"
    image = QImage(10, 10, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    assert image.save(str(source))
    archive = tmp_path / "board.revp"
    window.store = ProjectStore(archive)
    window.project = ProjectDocument("Project", "Board")
    monkeypatch.setattr(
        "tnasrevner.gui.QFileDialog.getOpenFileName",
        lambda *_args: (str(source), ""),
    )
    monkeypatch.setattr(window, "_choose_image_side", lambda: "top")
    monkeypatch.setattr(
        window,
        "_edit_imported_image",
        lambda image: (image, 10.0, (0.1, 0.2, 0.8, 0.2)),
    )

    window.import_picture()

    assert window.project.images[0].side == "top"
    assert window.store.read_asset("assets/top.png").startswith(b"\x89PNG")
    original_path = window.project.images[0].original_path
    assert original_path == "assets/original/top.png"
    assert window.store.read_asset(original_path).startswith(b"\x89PNG")
    assert window.project.images[0].calibration_line == (0.1, 0.2, 0.8, 0.2)


def test_import_editor_supports_free_rotation_and_zoom(app: QApplication) -> None:
    """Import editor applies arbitrary angle and preview zoom."""
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image))

    dialog._set_selection(QPoint(10, 10), QPoint(80, 60))
    dialog._rotate(30)
    dialog._zoom_by(2)

    assert dialog._angle == 30
    assert dialog._zoom == 2
    assert dialog._selection is not None
    assert dialog._source.width() > dialog._source.height()
    dialog.close()


def test_import_editor_keeps_selection_with_zoom(app: QApplication) -> None:
    """Zooming the editor keeps the crop on the same source pixels."""
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image))
    dialog._set_selection(QPoint(20, 10), QPoint(120, 70))
    before = dialog._source_rect(dialog._selection)

    dialog._zoom_by(2)

    after = dialog._source_rect(dialog._selection)
    assert abs(after.x() - before.x()) <= 1
    assert abs(after.y() - before.y()) <= 1
    assert abs(after.width() - before.width()) <= 1
    assert abs(after.height() - before.height()) <= 1
    dialog.close()


def test_import_editor_requires_scale_and_calculates_pixels_per_mm(
    app: QApplication,
) -> None:
    """Calibration line and real length produce persisted image scale."""
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image))
    dialog._set_calibration_line(QPoint(10, 10), QPoint(110, 10))
    dialog._millimeters.setValue(20)

    assert dialog.pixels_per_mm() == 5
    assert not dialog._buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dialog.close()


def test_import_editor_adjusts_rectangle_edge(app: QApplication) -> None:
    """One selection edge can be moved without replacing other edges."""
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image))
    dialog._set_selection(QPoint(10, 10), QPoint(80, 60))
    dialog._resize_edges = {"right"}

    dialog._resize_selection(QPoint(120, 60))

    assert dialog._selection.left() == 10
    assert dialog._selection.right() == 120
    assert dialog._selection.top() == 10
    assert dialog._selection.bottom() == 60
    dialog.close()


def test_remove_image_removes_selected_side(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remove image action removes metadata and archive asset."""
    archive = tmp_path / "board.revp"
    window.store = ProjectStore(archive)
    window.project = ProjectDocument("Project", "Board")
    window.store.write_asset("assets/top.png", b"picture")
    window.project.images.append(ImageAsset("top", "assets/top.png", "top.png"))
    monkeypatch.setattr(window, "_choose_image_side", lambda: "top")

    window.remove_picture()

    assert not window.project.images
    with pytest.raises(ProjectFormatError):
        window.store.read_asset("assets/top.png")
