"""Core GUI, board view, footprint, and schematic tests."""

# Qt uses compiled extension modules; test fixtures intentionally inspect GUI
# state to verify lifecycle behavior.
# pylint: disable=wrong-import-position,no-name-in-module,redefined-outer-name
# pylint: disable=unused-argument,protected-access,unused-import,too-many-lines
# pylint: disable=duplicate-code

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QValidator, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QMessageBox,
    QPushButton,
)

from tnasrevner.gui import (
    FootprintPickerDialog,
    ImageEditDialog,
    ImageView,
    MainWindow,
    SchematicCanvas,
    SchematicView,
)
from tnasrevner.kicad import CacheResult, FootprintReference, parse_footprint
from tnasrevner.project import (
    ComponentPin,
    Device,
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
def window(app: QApplication, tmp_path: Path) -> MainWindow:
    """Create isolated main window."""
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    result = MainWindow(show_startup=False, settings=settings)
    yield result
    result._dirty = False
    result.close()


def test_create_save_close_reopen_project(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise project creation, archive save, close, and reopen."""
    archive = tmp_path / "Project.revp"

    class FakeDialog:  # pylint: disable=too-few-public-methods
        """Replacement project dialog for non-interactive testing."""

        project_name = SimpleNamespace(text=lambda: "Project")
        description = SimpleNamespace(text=lambda: "Description")

        def __init__(self, _parent) -> None:
            pass

        def exec(self) -> QDialog.DialogCode:
            """Pretend user accepted project details."""
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr("tnasrevner.lib.project_io.ProjectDetailsDialog", FakeDialog)
    monkeypatch.setattr(window, "_last_project_directory", lambda: tmp_path)
    monkeypatch.setattr(window, "manage_picture", lambda: None)
    monkeypatch.setattr(
        "tnasrevner.lib.project_io.QFileDialog.getSaveFileName",
        lambda *_args: (str(archive), ""),
    )
    monkeypatch.setattr(
        "tnasrevner.lib.project_io.QFileDialog.getOpenFileName",
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
    assert window.project.board_name == "Description"


def test_main_window_has_application_icon(window: MainWindow) -> None:
    """Linux taskbars need a non-empty window icon."""
    assert not window.windowIcon().isNull()


def test_project_dialog_remembers_existing_directory(
    window: MainWindow, tmp_path: Path
) -> None:
    """Project dialogs reuse their last directory and recover when it vanishes."""
    window._settings.setValue("projects/last-directory", str(tmp_path))
    assert window._last_project_directory() == tmp_path

    missing = tmp_path / "removed"
    window._settings.setValue("projects/last-directory", str(missing))
    assert window._last_project_directory() == Path.home()


def test_close_project_cancel_preserves_dirty_project(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel in save guard must keep project and unsaved state."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    window._dirty = True
    monkeypatch.setattr(
        "tnasrevner.lib.project_io.QMessageBox.warning",
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
        "tnasrevner.lib.project_io.QFileDialog.getOpenFileName",
        lambda *_args: (str(invalid), ""),
    )
    monkeypatch.setattr(
        "tnasrevner.lib.project_io.QMessageBox.critical",
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


def test_image_view_zoom_keeps_center_and_footprint_scale(
    app: QApplication,
) -> None:
    """Scrollbar changes must not alter zoom ratio or footprint scale."""
    view = ImageView("No image")
    image = QImage(4000, 3000, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    view.resize(800, 600)
    view.show()
    view.set_pixmap(QPixmap.fromImage(image))
    footprint = parse_footprint(FOOTPRINT, "Resistor_SMD")
    view.set_device_placement(footprint, 20.0, "top", 0.0)
    app.processEvents()

    center = QPoint(view.viewport().width() // 2, view.viewport().height() // 2)
    old_effective = view._fit_scale() * view._scale
    old_label_point = view._label.mapFrom(view.viewport(), center)
    old_source = (
        old_label_point.x() / old_effective,
        old_label_point.y() / old_effective,
    )

    view._zoom_by(1.2)
    app.processEvents()

    center = QPoint(view.viewport().width() // 2, view.viewport().height() // 2)
    new_effective = view._fit_scale() * view._scale
    new_label_point = view._label.mapFrom(view.viewport(), center)
    new_source = (
        new_label_point.x() / new_effective,
        new_label_point.y() / new_effective,
    )
    assert new_effective / old_effective == pytest.approx(1.2)
    assert view._device_preview._effective_scale == pytest.approx(new_effective)
    assert new_source[0] == pytest.approx(old_source[0], abs=2)
    assert new_source[1] == pytest.approx(old_source[1], abs=2)
    view.close()


def test_image_view_zoom_has_full_view_minimum(app: QApplication) -> None:
    """Zooming out stops at the full cropped-image view."""
    view = ImageView("No image")
    image = QImage(1200, 800, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    view.resize(320, 240)
    view.show()
    view.set_pixmap(QPixmap.fromImage(image))
    app.processEvents()

    view._zoom_by(1 / 1.2)
    app.processEvents()

    assert view.zoom_ratio() == 1.0
    view.close()


def test_image_view_pan_sync_does_not_rerender_unchanged_scale(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synchronized pan changes reuse the already scaled board pixmap."""
    view = ImageView("No image")
    image = QImage(1200, 800, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    view.set_pixmap(QPixmap.fromImage(image))
    renders: list[bool] = []
    monkeypatch.setattr(view, "_render", lambda smooth=True: renders.append(smooth))

    view.apply_view_state((view._scale, 0.75, 0.25))

    assert not renders
    view.close()


def test_image_view_fast_zoom_preview_skips_vector_redraw(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive previews resize the composite and redraw vectors only at rest."""
    view = ImageView("No image")
    image = QImage(1200, 800, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    view.set_pixmap(QPixmap.fromImage(image))
    redraws: list[str] = []
    monkeypatch.setattr(
        view, "_draw_vector_footprints", lambda _pixmap: redraws.append("footprints")
    )
    monkeypatch.setattr(
        view, "_draw_vector_pads", lambda _pixmap: redraws.append("pads")
    )
    monkeypatch.setattr(
        view, "_draw_vector_pad_labels", lambda _pixmap: redraws.append("labels")
    )

    view._render(smooth=False)
    assert not redraws

    view._render(smooth=True)
    assert redraws == ["footprints", "pads", "labels"]
    view.close()


def test_board_view_sync_defers_hidden_views(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zooming one tab must not repaint the three hidden board views."""
    window._tabs.setCurrentIndex(0)
    source = window._views["top"]
    followers = (
        window._views["bottom"],
        window._side_views["top"],
        window._side_views["bottom"],
    )
    applied: list[ImageView] = []
    deferred: list[ImageView] = []
    for view in followers:
        monkeypatch.setattr(
            view,
            "apply_view_state",
            lambda _state, view=view: applied.append(view),
        )
        monkeypatch.setattr(
            view,
            "defer_view_state",
            lambda _state, view=view: deferred.append(view),
        )

    window._sync_board_views(source)

    assert not applied
    assert deferred == list(followers)


def test_image_view_mouse_and_zoom_mapping(app: QApplication) -> None:
    """Wheel zooms; pad clicks route actions; empty-space drag pans."""
    view = ImageView("No image")
    image = QImage(400, 400, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    pad = Pad("P1", "top", 0.4, 0.4, "pad-1", 0.2, 0.2)
    view.resize(240, 240)
    view.set_pixmap(QPixmap.fromImage(image))
    view.set_pad_labels((pad,))
    view.show()
    app.processEvents()

    selected: list[tuple[float, float]] = []
    connector: list[tuple[float, float]] = []
    menus: list[tuple[float, float]] = []
    net_edits: list[tuple[float, float]] = []
    view.pad_clicked.connect(lambda x, y: selected.append((x, y)))
    view.pad_connection_requested.connect(lambda x, y: connector.append((x, y)))
    view.pad_menu_requested.connect(lambda x, y: menus.append((x, y)))
    view.pad_context_requested.connect(lambda x, y: net_edits.append((x, y)))
    center = QPoint(view._label.width() // 2, view._label.height() // 2)

    QTest.mouseClick(view._label, Qt.MouseButton.LeftButton, pos=center)
    QTest.mouseClick(
        view._label,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ShiftModifier,
        center,
    )
    QTest.mouseClick(view._label, Qt.MouseButton.RightButton, pos=center)
    QTest.mouseClick(
        view._label,
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.ShiftModifier,
        center,
    )

    assert len(selected) == 1
    assert len(connector) == 1
    assert len(menus) == 1
    assert len(net_edits) == 1

    QTest.mouseDClick(view._label, Qt.MouseButton.LeftButton, pos=center)
    assert len(selected) == 2

    QTest.mousePress(view._label, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    assert view._drag_position is not None
    QTest.mouseRelease(view._label, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    assert len(selected) == 2

    old_scale = view._scale
    wheel = QWheelEvent(
        QPointF(center),
        QPointF(view._label.mapToGlobal(center)),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )
    view.wheelEvent(wheel)
    assert view._scale > old_scale
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


def test_save_and_restore_bom_view(window: MainWindow, tmp_path: Path) -> None:
    """The BOM tab is a persisted project display mode."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    window._tabs.setCurrentIndex(6)

    assert window.save_project()
    assert window.project.display.mode == "bom"

    window._tabs.setCurrentIndex(0)
    window._refresh_views()
    assert window._tabs.currentIndex() == 6


def test_save_and_restore_schematic_view(window: MainWindow, tmp_path: Path) -> None:
    """The Schematic tab is a persisted project display mode."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    window._tabs.setCurrentIndex(7)

    assert window.save_project()
    assert window.project.display.mode == "schematic"

    window._tabs.setCurrentIndex(0)
    window._refresh_views()
    assert window._tabs.currentIndex() == 7


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
        action.toolTip() == "Add a Pad"
        for action in window._tools_dock.widget().findChildren(QPushButton)
    )

    window.create_pad()
    window._views["top"].pad_selected.emit(0.6, 0.2, 0.15, 0.15)

    assert [pad.name for pad in window.project.pads] == ["P1", "P2"]


def test_add_device_rotates_and_creates_named_footprint_pads(
    window: MainWindow, tmp_path: Path
) -> None:
    """Placement creates pads and stays armed with the next unique reference."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    image = QImage(200, 200, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.project.images.append(
        ImageAsset(
            "top",
            "assets/top.png",
            "top.png",
            pixels_per_mm=1,
            calibration_line=(0.0, 0.5, 1.0, 0.5),
            calibration_length_mm=20,
        )
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
    assert view._device_placement
    assert window._pending_device is not None
    assert window._pending_device.reference == "R2"
    assert window._pending_device.rotation == 45

    window._place_device("top", 0.7, 0.5)

    assert [device.reference for device in window.project.devices] == ["R1", "R2"]
    assert len({device.reference for device in window.project.devices}) == 2
    assert [pad.name for pad in window.project.pads] == [
        "R1.1",
        "R1.2",
        "R2.1",
        "R2.2",
    ]
    assert window._pending_device is not None
    assert window._pending_device.reference == "R3"
    assert view._device_placement

    QTest.keyClick(window, Qt.Key.Key_Escape)

    assert window._pending_device is None
    assert not view._device_placement
    assert any(
        button.toolTip() == "Add Component"
        for button in window._tools_dock.widget().findChildren(QPushButton)
    )


def test_add_device_keeps_current_single_side_view(
    window: MainWindow, tmp_path: Path
) -> None:
    """Starting placement from Bottom must not force the side-by-side view."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    content = window._pixmap_bytes(QPixmap.fromImage(image))
    for side in ("top", "bottom"):
        path = f"assets/{side}.png"
        window.store.write_asset(path, content)
        window.project.images.append(
            ImageAsset(
                side,
                path,
                f"{side}.png",
                calibration_line=(0.0, 0.5, 1.0, 0.5),
                calibration_length_mm=10,
            )
        )
    window._refresh_views()
    window._tabs.setCurrentIndex(1)
    source = FootprintReference("Capacitor_SMD", "C_0603", tmp_path / "C.kicad_mod")
    footprint = parse_footprint(FOOTPRINT, source.library)

    window._begin_device_placement("C1", source, footprint, FOOTPRINT, "a" * 40)

    assert window._tabs.currentIndex() == 1
    assert window._views["bottom"]._device_placement
    assert not window._side_views["top"]._device_placement
    assert not window._side_views["bottom"]._device_placement

    window._place_device("bottom", 0.5, 0.5)

    assert window._tabs.currentIndex() == 1
    assert window.project.devices[0].side == "bottom"
    assert window._pending_device is not None
    assert window._pending_device.reference == "C2"
    assert window._views["bottom"]._device_placement
    window._cancel_device_placement()


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
        ImageAsset(
            "top",
            "assets/top.png",
            "top.png",
            pixels_per_mm=1,
            calibration_line=(0.0, 0.5, 1.0, 0.5),
            calibration_length_mm=10,
        )
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

        def __init__(self, _catalog, _parent, **_kwargs) -> None:
            order.append("footprint")

        @staticmethod
        def exec() -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        @staticmethod
        def selected_reference() -> FootprintReference:
            return source

    window._footprint_cache = FakeCache()
    monkeypatch.setattr(
        "tnasrevner.lib.footprint_actions.FootprintPickerDialog", FakePicker
    )

    def reference_popup(*_args, **_kwargs) -> tuple[str, bool]:
        order.append("reference")
        return "R1", True

    monkeypatch.setattr(
        "tnasrevner.lib.footprint_actions.QInputDialog.getText", reference_popup
    )

    window.add_device()

    assert order == ["footprint", "reference"]
    assert window._pending_device is not None
    assert window._pending_device.reference == "R1"


def test_tools_palette_buttons_have_icons_and_hover_help(window: MainWindow) -> None:
    """Every tool action exposes a visual label and mouse-over description."""
    buttons = window._tools_dock.widget().findChildren(QPushButton)
    assert buttons
    assert all(not button.icon().isNull() or button.text() for button in buttons)
    assert all(button.toolTip() for button in buttons)
    assert window._tools_dock.widget().findChild(QPushButton, "tool11").text() == "1:1"


def test_kicad_cache_failure_cancels_pending_import(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first-run cache failure prevents the pending device workflow."""
    warnings: list[str] = []
    window._add_device_pending = True
    monkeypatch.setattr(
        "tnasrevner.lib.footprint_actions.QMessageBox.warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    window._kicad_cache_failed("offline")

    assert not window._add_device_pending
    assert warnings == ["offline"]


def test_kicad_cache_warning_keeps_pending_import_alive(
    window: MainWindow,
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale-cache refresh failure warns but continues with valid cache."""
    warnings: list[str] = []
    opened: list[bool] = []
    window._add_device_pending = True
    monkeypatch.setattr(
        "tnasrevner.lib.footprint_actions.QMessageBox.warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    monkeypatch.setattr(window, "_open_footprint_picker", lambda: opened.append(True))

    window._kicad_cache_ready(
        CacheResult(
            tmp_path / "cache",
            "a" * 40,
            refreshed=False,
            warning="using existing cache",
        )
    )
    app.processEvents()

    assert warnings == ["using existing cache"]
    assert opened == [True]


def test_footprint_picker_pins_recent_choices_and_shows_preview(
    app: QApplication, tmp_path: Path
) -> None:
    """Recent footprints lead the picker and selecting one renders its geometry."""
    first_path = tmp_path / "R_0402.kicad_mod"
    recent_path = tmp_path / "R_0603.kicad_mod"
    first_path.write_bytes(FOOTPRINT)
    recent_path.write_bytes(FOOTPRINT)
    first = FootprintReference("Resistor_SMD", "R_0402", first_path)
    recent = FootprintReference("Resistor_SMD", "R_0603", recent_path)

    dialog = FootprintPickerDialog(
        (first, recent), recent_identifiers=(recent.identifier,)
    )
    app.processEvents()

    assert dialog.selected_reference() == recent
    assert dialog._list.item(0).text().startswith("★ ")
    preview = dialog._preview.pixmap()
    assert preview is not None and not preview.isNull()
    dialog.close()


def test_footprint_picker_pad_filter_works_without_index_and_keeps_search(
    app: QApplication, tmp_path: Path
) -> None:
    """Pad filtering falls back to footprint parsing and preserves search."""
    first_path = tmp_path / "R_0402.kicad_mod"
    second_path = tmp_path / "R_0603.kicad_mod"
    first_path.write_bytes(FOOTPRINT)
    second_path.write_bytes(
        FOOTPRINT.replace(
            b'(pad "2" smd rect (at 0.8 0) (size 1 1) (layers "F.Cu"))',
            b'(pad "2" smd rect (at 0.8 0) (size 1 1) (layers "F.Cu"))\n'
            b'  (pad "3" smd rect (at 1.8 0) (size 1 1) (layers "F.Cu"))',
        )
    )
    first = FootprintReference("Resistor_SMD", "R_0402", first_path)
    second = FootprintReference("Resistor_SMD", "R_0603", second_path)

    dialog = FootprintPickerDialog((first, second))
    dialog._search.setText("0603")
    dialog._pad_count.setValue(3)
    app.processEvents()

    assert dialog._list.count() == 1
    assert dialog.selected_reference() == second
    dialog.close()


def test_schematic_renders_all_symbol_families_with_schemdraw(
    app: QApplication,
) -> None:
    """Every schematic symbol family renders through Schemdraw anchors."""
    canvas = SchematicCanvas()
    image = QImage(500, 500, QImage.Format.Format_ARGB32)
    image.fill(0x20242B)
    pins = [ComponentPin(str(index), str(index)) for index in range(1, 9)]
    for kind, count in (
        ("resistor", 2),
        ("capacitor", 2),
        ("diode", 2),
        ("led", 2),
        ("battery", 2),
        ("switch", 2),
        ("connector", 4),
        ("transistor", 3),
        ("uc", 8),
        ("pad", 1),
    ):
        painter = QPainter(image)
        endpoints = canvas._draw_schemdraw_symbol(painter, kind, pins[:count])
        painter.end()
        assert len(endpoints) == count

    cache_key = ("uc", tuple(pin.number for pin in pins))
    cached_renderer = canvas._schemdraw_cache[cache_key][0]
    painter = QPainter(image)
    endpoints = canvas._draw_schemdraw_symbol(painter, "uc", pins)
    painter.end()
    assert len(endpoints) == len(pins)
    assert canvas._schemdraw_cache[cache_key][0] is cached_renderer
    canvas.set_zoom(100)
    assert canvas._zoom == SchematicCanvas.MAX_ZOOM


def test_schematic_fit_keeps_zoom_state_for_next_zoom(app: QApplication) -> None:
    """Zoom after FIT uses the canvas zoom instead of a stale viewport value."""
    view = SchematicView()
    view.resize(700, 500)
    view.show()
    app.processEvents()

    view.fit_overview()
    app.processEvents()
    fitted_zoom = view._canvas._zoom
    assert view._zoom == fitted_zoom

    view._zoom_by(1.2, QPoint(200, 150))

    assert view._zoom == pytest.approx(fitted_zoom * 1.2)
    assert view._canvas._zoom == view._zoom
    view.close()


def test_recent_footprints_persist_newest_five(
    window: MainWindow, tmp_path: Path
) -> None:
    """The footprint MRU list remains unique and is capped at five entries."""
    sources = [
        FootprintReference("Library", f"Part_{index}", tmp_path / f"{index}.kicad_mod")
        for index in range(7)
    ]

    for source in sources:
        window._remember_recent_footprint(source)
    window._remember_recent_footprint(sources[4])

    assert window._recent_footprint_identifiers() == tuple(
        source.identifier
        for source in (sources[4], sources[6], sources[5], sources[3], sources[2])
    )


def test_device_reference_is_suggested_and_incremented_by_family(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capacitor choices propose C1, then the next unused sequential reference."""
    window.project = ProjectDocument("Project", "Board")
    source = FootprintReference(
        "Capacitor_SMD", "C_0603", tmp_path / "C_0603.kicad_mod"
    )
    suggestions: list[str] = []

    def reference_popup(*_args, **kwargs) -> tuple[str, bool]:
        suggestions.append(kwargs["text"])
        return kwargs["text"], True

    monkeypatch.setattr(
        "tnasrevner.lib.footprint_actions.QInputDialog.getText", reference_popup
    )

    assert window._ask_device_reference(source) == "C1"
    window.project.devices.append(
        Device(
            "C1",
            "top",
            0.5,
            0.5,
            source.library,
            source.name,
            "assets/kicad/c1.kicad_mod",
            "a" * 40,
        )
    )
    assert window._ask_device_reference(source) == "C2"
    assert suggestions == ["C1", "C2"]

    window.project.devices.append(
        Device(
            "C3",
            "top",
            0.6,
            0.5,
            source.library,
            source.name,
            "assets/kicad/c3.kicad_mod",
            "a" * 40,
        )
    )
    assert window._next_device_reference(source) == "C2"


def test_unknown_device_reference_uses_ic_prefix(
    window: MainWindow, tmp_path: Path
) -> None:
    """Non-resistor/capacitor footprints use IC1, IC2 references."""
    window.project = ProjectDocument("Project", "Board")
    source = FootprintReference(
        "Custom_Logic", "Mystery", tmp_path / "mystery.kicad_mod"
    )

    assert window._next_device_reference(source) == "IC1"
    window.project.devices.append(
        Device(
            "IC1",
            "top",
            0.5,
            0.5,
            source.library,
            source.name,
            "assets/kicad/u1.kicad_mod",
            "a" * 40,
        )
    )
    assert window._next_device_reference(source) == "IC2"


def test_add_device_requires_saved_measurement_for_legacy_image(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A derived legacy number alone cannot guarantee physical device size."""
    window.store = ProjectStore(tmp_path / "legacy.revp")
    window.project = ProjectDocument(
        "Project",
        "Board",
        images=[ImageAsset("top", "assets/top.png", "top.png", pixels_per_mm=3.199)],
    )
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    messages: list[str] = []
    monkeypatch.setattr(
        "tnasrevner.lib.footprint_actions.QMessageBox.information",
        lambda _parent, _title, message: messages.append(message),
    )

    window.add_device()

    assert messages and "legacy scale data" in messages[0]
    assert not window._add_device_pending


def test_recalibration_rebuilds_existing_device_pads(
    window: MainWindow, tmp_path: Path
) -> None:
    """Changing the saved measurement updates generated pad geometry."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    image = QImage(200, 200, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    pixmap = QPixmap.fromImage(image)
    window.store.write_asset("assets/top.png", window._pixmap_bytes(pixmap))
    window.project.images.append(
        ImageAsset(
            "top",
            "assets/top.png",
            "top.png",
            pixels_per_mm=10,
            calibration_line=(0.0, 0.5, 1.0, 0.5),
            calibration_length_mm=20,
        )
    )
    source = FootprintReference("Resistor_SMD", "R_0603", tmp_path / "R.kicad_mod")
    footprint = parse_footprint(FOOTPRINT, source.library)
    window._begin_device_placement("R1", source, footprint, FOOTPRINT, "a" * 40)
    window._place_device("top", 0.5, 0.5)
    original_pad_id = window.project.pads[0].pad_id

    window.project.images[0] = ImageAsset(
        "top",
        "assets/top.png",
        "top.png",
        pixels_per_mm=10,
        calibration_line=(0.0, 0.5, 1.0, 0.5),
        calibration_length_mm=10,
    )
    window._rebuild_device_pads("top", pixmap)

    assert window.project.pads[0].width * image.width() == pytest.approx(20)
    assert window.project.pads[0].pad_id == original_pad_id


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
    assert window._schematic_view._canvas._selected_net == "GND"
    assert window._views["top"]._scale == 3.0

    window._select_pad("top", 0.55, 0.55)
    assert window._selected_pad_id is None
    assert window._views["top"]._scale == 3.0

    view = window._views["top"]
    window.show()
    QApplication.processEvents()
    point = QPoint(
        round(0.55 * (view._label.width() - 1)),
        round(0.55 * (view._label.height() - 1)),
    )
    QTest.mouseClick(view._label, Qt.MouseButton.LeftButton, pos=point)
    QApplication.processEvents()
    assert window._selected_pad_id == "two"
    point = QPoint(
        round(0.55 * (view._label.width() - 1)),
        round(0.55 * (view._label.height() - 1)),
    )
    QTest.mouseDClick(view._label, Qt.MouseButton.LeftButton, pos=point)
    QApplication.processEvents()
    assert window._selected_pad_id is None
    assert window._selected_net is None

    window._set_pads_visible(False)
    QApplication.processEvents()
    assert not window._pads_visible
    assert window._views["top"]._scale == 3.0
