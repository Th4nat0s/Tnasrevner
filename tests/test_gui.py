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
from tnasrevner.kicad import FootprintReference, parse_footprint
from tnasrevner.project import (
    ImageAsset,
    Pad,
    ProjectDocument,
    ProjectFormatError,
    ProjectStore,
)

FOOTPRINT = b"""(footprint "R_0603"
  (version 20240108) (generator test) (layer "F.Cu")
  (fp_rect (start -1.5 -0.8) (end 1.5 0.8)
    (stroke (width 0.15) (type default)) (fill none) (layer "F.SilkS"))
  (pad "1" smd rect (at -0.8 0) (size 1 1) (layers "F.Cu"))
  (pad "2" smd rect (at 0.8 0) (size 1 1) (layers "F.Cu")))"""


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


def test_add_device_rotates_and_creates_named_footprint_pads(
    window: MainWindow, tmp_path: Path
) -> None:
    """Right-click rotates preview; left-click creates device and its pads."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    image = QImage(200, 200, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.project.images.append(
        ImageAsset("top", "assets/top.png", "top.png", pixels_per_mm=10)
    )
    window._refresh_views()
    source = FootprintReference("Resistor_SMD", "R_0603", tmp_path / "R_0603.kicad_mod")
    footprint = parse_footprint(FOOTPRINT, source.library)

    window._begin_device_placement("R1", source, footprint, FOOTPRINT, "a" * 40)
    view = window._views["top"]
    QTest.mouseClick(
        view._label,
        Qt.MouseButton.RightButton,
        pos=QPoint(view._label.width() // 2, view._label.height() // 2),
    )
    QTest.mouseClick(
        view._label,
        Qt.MouseButton.LeftButton,
        pos=QPoint(view._label.width() // 2, view._label.height() // 2),
    )

    assert len(window.project.devices) == 1
    assert window.project.devices[0].reference == "R1"
    assert window.project.devices[0].rotation == 45
    assert [pad.name for pad in window.project.pads] == ["R1.1", "R1.2"]
    assert window.project.pads[0].width * image.width() == pytest.approx(10)
    assert window.project.pads[0].height * image.height() == pytest.approx(10)
    assert all(
        pad.device_id == window.project.devices[0].device_id
        for pad in window.project.pads
    )
    assert (
        window.store.read_asset(window.project.devices[0].footprint_path) == FOOTPRINT
    )
    assert not view._device_placement
    assert any(
        button.text() == "Add device"
        for button in window._tools_dock.widget().findChildren(QPushButton)
    )


def test_add_device_selects_footprint_before_asking_reference(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tools workflow opens KiCad selection before the reference popup."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.project.images.append(
        ImageAsset("top", "assets/top.png", "top.png", pixels_per_mm=10)
    )
    window._refresh_views()
    source = FootprintReference("Resistor_SMD", "R_0603", tmp_path / "R_0603.kicad_mod")
    footprint = parse_footprint(FOOTPRINT, source.library)
    order: list[str] = []

    class FakeCache:  # pylint: disable=too-few-public-methods,missing-function-docstring
        """Provide one ready footprint without network access."""

        @staticmethod
        def is_ready_and_fresh() -> bool:
            return True

        @staticmethod
        def current_revision() -> str:
            return "a" * 40

        @staticmethod
        def catalog() -> tuple[FootprintReference, ...]:
            return (source,)

        @staticmethod
        def load(_source: FootprintReference) -> tuple[object, bytes]:
            return footprint, FOOTPRINT

    class FakePicker:  # pylint: disable=too-few-public-methods,missing-function-docstring
        """Record that footprint selection happened first."""

        def __init__(self, _catalog, _parent) -> None:
            order.append("footprint")

        @staticmethod
        def exec() -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        @staticmethod
        def selected_reference() -> FootprintReference:
            return source

    window._footprint_cache = FakeCache()
    monkeypatch.setattr("tnasrevner.gui.FootprintPickerDialog", FakePicker)

    def reference_popup(*_args) -> tuple[str, bool]:
        order.append("reference")
        return "R1", True

    monkeypatch.setattr("tnasrevner.gui.QInputDialog.getText", reference_popup)

    window.add_device()

    assert order == ["footprint", "reference"]
    assert window._pending_device is not None
    assert window._pending_device.reference == "R1"


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

    window._select_pad("top", 0.55, 0.55)
    assert window._selected_pad_id == "two"
    assert window._selected_net == "GND"
    assert window._views["top"]._scale == 3.0

    window._select_pad("top", 0.55, 0.55)
    assert window._selected_pad_id is None
    assert window._views["top"]._scale == 3.0

    window._set_pads_visible(False)
    QApplication.processEvents()
    assert not window._pads_visible
    assert window._views["top"]._scale == 3.0


def test_net_view_and_right_click_assignment(window: MainWindow) -> None:
    """Net assignment keeps pad name and appears in the Nets table."""
    window.project = ProjectDocument(
        "Project", "Board", pads=[Pad("P1", "top", 0.1, 0.1, "one", 0.1, 0.1)]
    )
    window._assign_pad_net("one", "GND")
    QApplication.processEvents()
    window._refresh_net_table()

    assert window.project.pads[0].name == "P1"
    assert window.project.pads[0].net == "GND"
    assert window._net_table.item(0, 0).text() == "P1"
    assert window._net_table.item(0, 1).text() == "GND"

    window._connect_pad_to_net("top", 0.15, 0.15)
    assert window._net_dialog is not None
    window._net_dialog.reject()
    QApplication.processEvents()
    assert window._net_dialog is None


def test_pad_refresh_reuses_cached_working_image(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated pad refreshes decode each working image only once."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.project.images.append(ImageAsset("top", "assets/top.png", "top.png"))
    original_read = window.store.read_asset
    reads = 0

    def counted_read(path: str) -> bytes:
        nonlocal reads
        reads += 1
        return original_read(path)

    monkeypatch.setattr(window.store, "read_asset", counted_read)
    window._image_cache.clear()

    window._refresh_views()
    window._refresh_views()

    assert reads == 1


def test_shift_click_pad_menu_disconnects_and_deletes(window: MainWindow) -> None:
    """Pad menu exposes net and deletion actions without a modal loop."""
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[Pad("P1", "top", 0.1, 0.1, "one", 0.1, 0.1, "GND")],
    )

    window._show_pad_menu("top", 0.15, 0.15)
    assert window._pad_menu is not None
    actions = {action.text(): action for action in window._pad_menu.actions()}
    assert set(actions) == {"Delete pad", "Connect to net", "Disconnect"}
    assert actions["Disconnect"].isEnabled()
    actions["Disconnect"].trigger()
    assert window.project.pads[0].net is None
    if window._pad_menu is not None:
        window._pad_menu.close()

    window._show_pad_menu("top", 0.15, 0.15)
    assert window._pad_menu is not None
    actions = {action.text(): action for action in window._pad_menu.actions()}
    actions["Delete pad"].trigger()
    assert not window.project.pads
    if window._pad_menu is not None:
        window._pad_menu.close()


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


def test_editing_existing_image_preserves_physical_scale(app: QApplication) -> None:
    """FIT display scaling must not alter persisted source pixels per mm."""
    image = QImage(2000, 1000, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image))
    dialog.resize(800, 600)
    dialog._render()
    assert dialog._display_scale < 1

    dialog.prepare_existing_image(25.0, (0.1, 0.5, 0.6, 0.5))

    assert dialog.pixels_per_mm() == pytest.approx(25.0, rel=0.01)
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
