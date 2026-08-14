"""Core GUI, board view, footprint, and schematic tests."""

# Qt uses compiled extension modules; test fixtures intentionally inspect GUI
# state to verify lifecycle behavior.
# pylint: disable=wrong-import-position,no-name-in-module,redefined-outer-name
# pylint: disable=unused-argument,protected-access,unused-import,too-many-lines
# pylint: disable=duplicate-code

import os
from dataclasses import replace
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QLineF, QPoint, QPointF, QRectF, QSettings, Qt
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
from tnasrevner.lib.schematic_layout import SchematicLayoutOptimizer
from tnasrevner.lib.schematic_router import OrthogonalRouter
from tnasrevner.project import (
    ComponentPin,
    Device,
    FootprintDefinition,
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


def test_save_as_copies_assets_switches_store_and_preserves_source(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Save As copies project assets and makes later Save target new archive."""
    source = tmp_path / "source.revp"
    target = tmp_path / "renamed-project"
    image = QImage(20, 20, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    window.store = ProjectStore(source)
    window.project = ProjectDocument(
        "Project",
        "Board",
        images=[ImageAsset("top", "assets/top.png", "top.png")],
        devices=[
            Device(
                "C1",
                "top",
                0.5,
                0.5,
                "Resistor_SMD",
                "R_0603",
                "assets/kicad/c1.kicad_mod",
                "a" * 40,
            )
        ],
    )
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.store.write_asset("assets/kicad/c1.kicad_mod", FOOTPRINT)
    window.store.save(window.project)
    original = source.read_bytes()
    monkeypatch.setattr(
        "tnasrevner.lib.project_io.QFileDialog.getSaveFileName",
        lambda *_args: (str(target), ""),
    )

    assert window.save_project_as()
    target_path = target.with_suffix(".revp")
    assert window.store.path == target_path
    assert target_path.is_file()
    assert source.read_bytes() == original
    copied = ProjectStore(target_path)
    saved = copied.load()
    assert saved.project_name == "Project"
    assert copied.read_asset("assets/top.png")
    assert copied.read_asset("assets/kicad/c1.kicad_mod") == FOOTPRINT

    window.project.devices[0] = replace(window.project.devices[0], value="10 nF")
    window._dirty = True
    assert window.save_project()
    assert ProjectStore(target_path).load().devices[0].value == "10 nF"

    window._dirty = True
    monkeypatch.setattr(
        "tnasrevner.lib.project_io.QFileDialog.getSaveFileName",
        lambda *_args: ("", ""),
    )
    assert not window.save_project_as()
    assert window.store.path == target_path
    assert window._dirty


def test_save_as_recovers_missing_footprint_from_kicad_cache(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Save As restores a missing shared footprint from the local KiCad cache."""
    source = tmp_path / "source.revp"
    target = tmp_path / "recovered"
    definition = FootprintDefinition.identity("Resistor_SMD", "R_0603", FOOTPRINT)
    window.store = ProjectStore(source)
    window.project = ProjectDocument(
        "Project",
        "Board",
        devices=[
            Device(
                "R1",
                "top",
                0.5,
                0.5,
                "Resistor_SMD",
                "R_0603",
                f"assets/kicad/{definition}.kicad_mod",
                "a" * 40,
                footprint_definition_id=definition,
            )
        ],
        footprint_definitions=[
            FootprintDefinition(
                definition,
                "Resistor_SMD",
                "R_0603",
                f"assets/kicad/{definition}.kicad_mod",
                "a" * 40,
                FootprintDefinition.hash_content(FOOTPRINT),
            )
        ],
    )
    monkeypatch.setattr(
        "tnasrevner.lib.project_io.QFileDialog.getSaveFileName",
        lambda *_args: (str(target), ""),
    )
    monkeypatch.setattr(
        window._footprint_cache,
        "catalog",
        lambda: (FootprintReference("Resistor_SMD", "R_0603", tmp_path / "R"),),
    )
    monkeypatch.setattr(
        window._footprint_cache,
        "load",
        lambda _reference: (None, FOOTPRINT),
    )

    assert window.save_project_as()
    assert ProjectStore(target.with_suffix(".revp")).read_asset(
        f"assets/kicad/{definition}.kicad_mod"
    ) == FOOTPRINT


def test_save_as_actions_are_exposed(window: MainWindow) -> None:
    """File menu and toolbar expose explicit Save As controls."""
    file_action = next(
        action for action in window.menuBar().actions() if action.text() == "File"
    )
    file_menu = file_action.menu()
    save_as = next(
        action for action in file_menu.actions() if action.text() == "Save project as…"
    )
    assert save_as.shortcut().toString() == "Ctrl+Shift+S"
    assert window.findChild(QPushButton, "toolSaveAs") is None


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


def test_schematic_viewport_survives_tab_switch_and_save(
    window: MainWindow, tmp_path: Path
) -> None:
    """Schematic zoom and scroll persist when saving from another tab."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    window._tabs.setCurrentIndex(7)
    QApplication.processEvents()
    window._schematic_view.apply_view_state((1.75, 123, 456))

    window._tabs.setCurrentIndex(0)
    QApplication.processEvents()
    assert window.project.display.schematic_zoom == pytest.approx(1.75)
    assert window.project.display.schematic_pan_x == 123.0
    assert window.project.display.schematic_pan_y == 456.0
    assert window.save_project()

    loaded = ProjectStore(tmp_path / "board.revp").load()
    assert loaded.display.schematic_zoom == pytest.approx(1.75)
    assert loaded.display.schematic_pan_x == 123.0
    assert loaded.display.schematic_pan_y == 456.0

    window._schematic_view.apply_view_state((1.0, 0, 0))
    window._tabs.setCurrentIndex(7)
    QApplication.processEvents()
    assert window._schematic_view.view_state() == (1.75, 123, 456)


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
    window._views["bottom"].apply_view_state((2.25, 0.8, 0.2))
    before_placement = window._views["bottom"].view_state()
    source = FootprintReference("Capacitor_SMD", "C_0603", tmp_path / "C.kicad_mod")
    footprint = parse_footprint(FOOTPRINT, source.library)

    window._begin_device_placement("C1", source, footprint, FOOTPRINT, "a" * 40)

    assert window._tabs.currentIndex() == 1
    assert window._views["bottom"]._device_placement
    assert not window._side_views["top"]._device_placement
    assert not window._side_views["bottom"]._device_placement

    window._place_device("bottom", 0.5, 0.5)

    assert window._tabs.currentIndex() == 1
    assert window._views["bottom"].view_state() == pytest.approx(before_placement)
    assert window.project.devices[0].side == "bottom"
    assert window._pending_device is not None
    assert window._pending_device.reference == "C2"
    assert window._views["bottom"]._device_placement
    window._place_device("bottom", 0.6, 0.6)
    QApplication.processEvents()
    assert len(window.project.devices) == 2
    assert window._views["bottom"].view_state() == pytest.approx(before_placement)
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
    ruler = window._tools_dock.widget().findChild(QPushButton, "toolRuler")
    assert ruler is not None
    assert ruler.text() == "📐"
    assert ruler.accessibleName() == "Ruler"
    assert ruler.toolTip() == "Measure Tool"
    assert ruler.statusTip() == "Measure Tool"


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

    cache_key = ("uc", tuple(pin.number for pin in pins), "#e7edf5")
    cached_renderer = canvas._schemdraw_cache[cache_key][0]
    painter = QPainter(image)
    endpoints = canvas._draw_schemdraw_symbol(painter, "uc", pins)
    painter.end()
    assert len(endpoints) == len(pins)
    assert canvas._schemdraw_cache[cache_key][0] is cached_renderer
    canvas.set_zoom(100)
    assert canvas._zoom == SchematicCanvas.MAX_ZOOM


def test_schemdraw_two_terminal_anchors_touch_rendered_symbol(
    app: QApplication,
) -> None:
    """Two-terminal anchors remain aligned with the rendered resistor leads."""
    del app
    canvas = SchematicCanvas()
    image = QImage(400, 400, QImage.Format.Format_ARGB32)
    background = QColor("#20242b")
    image.fill(background)
    painter = QPainter(image)
    painter.translate(200, 200)
    endpoints = canvas._draw_schemdraw_symbol(
        painter,
        "resistor",
        [ComponentPin("1", "1"), ComponentPin("2", "2")],
    )
    painter.end()

    for endpoint in endpoints:
        x = round(200 + endpoint.x())
        assert any(
            image.pixelColor(candidate, 200) != background
            for candidate in range(x - 2, x + 3)
        )


def test_schemdraw_ic_anchors_touch_rendered_pins(app: QApplication) -> None:
    """Multi-pin IC anchors use same SVG coordinate frame as white leads."""
    del app
    canvas = SchematicCanvas()
    image = QImage(400, 400, QImage.Format.Format_ARGB32)
    background = QColor("#20242b")
    image.fill(background)
    painter = QPainter(image)
    painter.translate(200, 200)
    endpoints = canvas._draw_schemdraw_symbol(
        painter,
        "uc",
        [ComponentPin(str(index), str(index)) for index in range(1, 9)],
    )
    painter.end()

    for endpoint in endpoints:
        x = round(200 + endpoint.x())
        y = round(200 + endpoint.y())
        assert any(
            image.pixelColor(candidate_x, candidate_y) != background
            for candidate_x in range(x - 2, x + 3)
            for candidate_y in range(y - 2, y + 3)
        )


def test_schemdraw_cache_separates_glued_foreground(app: QApplication) -> None:
    """Schemdraw symbols with different glued colors use separate renderers."""
    del app
    canvas = SchematicCanvas()
    pins = [ComponentPin("1", "1"), ComponentPin("2", "2")]
    image = QImage(400, 400, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    canvas._draw_schemdraw_symbol(painter, "resistor", pins)
    canvas._draw_schemdraw_symbol(painter, "resistor", pins, QColor("#f5a3c7"))
    painter.end()

    normal_key = ("resistor", ("1", "2"), "#e7edf5")
    glued_key = ("resistor", ("1", "2"), "#f5a3c7")
    assert normal_key in canvas._schemdraw_cache
    assert glued_key in canvas._schemdraw_cache
    assert canvas._schemdraw_cache[normal_key][0] is not canvas._schemdraw_cache[
        glued_key
    ][0]


def test_schematic_net_wire_stays_visible_at_overview_zoom(
    app: QApplication,
) -> None:
    """Overview zoom keeps a connected net above sub-pixel width."""
    del app
    first = Device(
        "R1",
        "top",
        0.0,
        0.0,
        "Resistor",
        "R",
        "assets/kicad/r1.kicad_mod",
        "revision",
        device_id="r1",
        pins=[
            ComponentPin("1", "1", net_id="NT1"),
            ComponentPin("2", "2"),
        ],
        schematic_x=5000.0,
        schematic_y=5000.0,
    )
    second = replace(
        first,
        reference="R2",
        device_id="r2",
        pins=[
            ComponentPin("1", "1", net_id="NT1"),
            ComponentPin("2", "2"),
        ],
        schematic_x=7000.0,
        schematic_y=5000.0,
    )
    canvas = SchematicCanvas()
    canvas._project = ProjectDocument("Project", "Board", devices=[first, second])
    canvas.resize(1000, 700)
    canvas.set_zoom(SchematicCanvas.MIN_ZOOM)
    image = QImage(1000, 700, QImage.Format.Format_ARGB32)
    image.fill(QColor("#20242b"))
    painter = QPainter(image)
    canvas.render(painter, QPoint(0, 0))
    painter.end()

    assert canvas._net_pen(False).isCosmetic()


def test_schematic_interactive_paint_has_100x_regression_budget(
    app: QApplication,
) -> None:
    """Interactive DakeFPV paint must stay below 100x the 10 ms baseline."""
    del app
    project = ProjectStore(Path("Sample_Img/DakeFVP2.revp")).load()
    canvas = SchematicCanvas()
    canvas._project = project
    canvas.resize(1600, 1000)
    canvas._drag_device_id = project.devices[0].device_id
    image = QImage(1600, 1000, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    started = monotonic()
    canvas.render(painter, QPoint(0, 0))
    elapsed = monotonic() - started
    painter.end()

    assert elapsed < 1.0, f"interactive schematic paint too slow: {elapsed:.3f}s"


def test_schematic_optimizer_has_sparse_dake_performance_budget(
    app: QApplication,
) -> None:
    """Sparse DakeFPV optimization must remain bounded below three seconds."""
    del app
    project = ProjectStore(Path("Sample_Img/DakeFVP2.revp")).load()
    canvas = SchematicCanvas()
    canvas._project = project
    canvas._auto_centers = canvas._layout_devices()
    positions = {
        device.device_id: canvas._center_for_device(device, index)
        for index, device in enumerate(project.devices)
    }
    rotations = {
        device.device_id: device.schematic_rotation for device in project.devices
    }
    started = monotonic()

    SchematicLayoutOptimizer.optimize(
        project.devices,
        positions,
        rotations,
        canvas._layout_edges(project.devices),
        canvas._layout_connections(project.devices),
        canvas._WORLD_SIZE,
        canvas._symbol_size,
    )
    elapsed = monotonic() - started

    assert elapsed < 3.0, f"schematic optimization too slow: {elapsed:.3f}s"
    bounds = [
        SchematicLayoutOptimizer._candidate_bounds(
            device,
            positions[device.device_id],
            rotations,
            canvas._symbol_size,
        )
        for device in project.devices
    ]
    assert not any(
        left.intersects(right)
        for index, left in enumerate(bounds)
        for right in bounds[index + 1 :]
    )


def test_schematic_power_net_aliases_share_global_symbol() -> None:
    """Power aliases group case-insensitively for schematic rendering."""
    assert SchematicCanvas._power_net_label("gNd") == "GND"
    assert SchematicCanvas._power_net_label("VBATT") == "VBAT"
    assert SchematicCanvas._net_group_key("vbat") == (
        SchematicCanvas._net_group_key("vbatt")
    )
    assert (
        SchematicCanvas._net_display_name(SchematicCanvas._net_group_key("v3.3"))
        == "V3.3"
    )


def test_schematic_multiterminal_net_uses_short_spanning_tree() -> None:
    """Multi-terminal routing avoids a long star from the first terminal."""
    points = [
        QPointF(0, 0),
        QPointF(1000, 0),
        QPointF(1000, 100),
        QPointF(1000, 200),
    ]
    pairs = SchematicCanvas._minimum_spanning_pairs(points)
    tree_length = sum(
        abs(points[left].x() - points[right].x())
        + abs(points[left].y() - points[right].y())
        for left, right in pairs
    )
    star_length = sum(
        abs(points[0].x() - point.x()) + abs(points[0].y() - point.y())
        for point in points[1:]
    )

    assert len(pairs) == len(points) - 1
    assert tree_length < star_length


def test_schematic_router_keeps_every_segment_orthogonal() -> None:
    """Off-grid terminal adapters never create diagonal wire segments."""
    path = OrthogonalRouter.route(
        QPointF(13, 17),
        QPointF(191, 143),
        (QRectF(80, 40, 50, 80),),
    )

    assert all(
        start.x() == end.x() or start.y() == end.y()
        for start, end in zip(path, path[1:])
    )


def test_schematic_router_simplifies_clear_connection_to_one_corner() -> None:
    """Clear off-grid terminals use one short L instead of grid detours."""
    path = OrthogonalRouter.route(QPointF(380, 184), QPointF(58, 180))

    assert path == [QPointF(380, 184), QPointF(380, 180), QPointF(58, 180)]


def test_schematic_router_prefers_non_overlapping_l_route() -> None:
    """Direct routing chooses a longer first leg instead of sharing a trunk."""
    existing = ((QPointF(100, 0), QPointF(100, 100)),)

    path = OrthogonalRouter.route(
        QPointF(100, -10),
        QPointF(200, 50),
        existing=existing,
    )

    assert path == [QPointF(100, -10), QPointF(200, -10), QPointF(200, 50)]


def test_schematic_net_label_uses_longest_route_segment() -> None:
    """NET text stays away from a crowded L-route bend."""
    point = SchematicCanvas._route_label_point(
        [QPointF(100, -10), QPointF(200, -10), QPointF(200, 50)]
    )

    assert point == QPointF(150, -10)


def test_schematic_display_lanes_do_not_touch_at_one_to_one() -> None:
    """Parallel nets keep visible stroke clearance without logical rerouting."""
    occupied = ((QPointF(100, 0), QPointF(100, 100)),)
    display_path = SchematicCanvas._display_lane_path(
        [QPointF(100, 0), QPointF(100, 100)],
        occupied,
        SchematicCanvas._NET_LINE_WIDTH + 2.0,
    )
    segments = tuple(zip(display_path, display_path[1:]))

    assert display_path[0] == QPointF(100, 0)
    assert display_path[-1] == QPointF(100, 100)
    assert all(start.x() == end.x() or start.y() == end.y() for start, end in segments)
    assert not any(
        SchematicCanvas._parallel_segments_conflict(
            segment,
            occupied[0],
            SchematicCanvas._NET_LINE_WIDTH + 2.0,
        )
        for segment in segments
    )


def test_independent_pad_auto_layout_uses_board_perimeter(
    app: QApplication,
) -> None:
    """Unplaced independent pads spread around schematic by board edge."""
    del app
    canvas = SchematicCanvas()
    top = Pad("TOP", "top", 0.5, 0.01, "top-pad")
    bottom = Pad("BOTTOM", "top", 0.5, 0.95, "bottom-pad")
    left = Pad("LEFT", "top", 0.01, 0.5, "left-pad")
    right = Pad("RIGHT", "top", 0.95, 0.5, "right-pad")
    canvas._project = ProjectDocument(
        "Project", "Board", pads=[top, bottom, left, right]
    )
    points = {
        pad.name: canvas._independent_pad_center(index, 4, pad)
        for index, pad in enumerate((top, bottom, left, right))
    }

    assert points["TOP"].y() < points["BOTTOM"].y()
    assert points["LEFT"].x() < points["RIGHT"].x()
    assert len({(point.x(), point.y()) for point in points.values()}) == 4


def test_connected_independent_pad_stays_outside_large_ic(
    app: QApplication,
) -> None:
    """Automatic pad placement never puts a terminal inside an IC body."""
    del app
    device = Device(
        "IC1",
        "top",
        0.5,
        0.5,
        "Package_QFP",
        "QFP_64",
        "assets/kicad/ic1.kicad_mod",
        "revision",
        device_id="ic1",
        pins=[
            ComponentPin(str(index), str(index), net_id="SIG" if index == 1 else None)
            for index in range(1, 65)
        ],
        schematic_x=10_000.0,
        schematic_y=10_000.0,
    )
    pad = Pad("P1", "top", 0.5, 0.5, "pad", net="SIG")
    canvas = SchematicCanvas()
    canvas._project = ProjectDocument("Project", "Board", devices=[device], pads=[pad])
    point = canvas._independent_pad_center(0, 1, pad)

    assert not canvas._device_bounds(device, QPointF(10_000, 10_000)).contains(point)


def test_connected_pad_is_placed_at_exact_pin_exit(
    app: QApplication,
) -> None:
    """Automatic pad placement follows connected pin endpoint and direction."""
    del app
    pad = Pad("P52", "top", 0.5, 0.5, "pad-52", net="NT1")
    canvas = SchematicCanvas()
    canvas._project = ProjectDocument("Project", "Board", pads=[pad])
    center, outward = canvas._pad_render_geometry(
        0,
        1,
        pad,
        {"net:NT1": [(QPointF(100, 200), QPointF(0, 1))]},
    )

    assert center == QPointF(100, 310)
    assert outward == QPointF(0, -1)


def test_pad_only_net_is_compacted_before_routing(app: QApplication) -> None:
    """Automatic pads sharing a pad-only net cannot create a giant wire."""
    del app
    pads = [
        Pad("P12", "top", 0.1, 0.1, "pad-12", net="LED"),
        Pad("P19", "top", 0.1, 0.9, "pad-19", net="LED"),
        Pad("P20", "top", 0.9, 0.5, "pad-20"),
    ]
    canvas = SchematicCanvas()
    canvas._project = ProjectDocument("Project", "Board", pads=pads)

    centers = canvas._pad_only_net_centers({})

    assert set(centers) == {"pad-12", "pad-19"}
    assert QLineF(centers["pad-12"], centers["pad-19"]).length() == 90.0


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


def test_schematic_optimizer_preserves_glued_devices(app: QApplication) -> None:
    """Optimization moves free devices but preserves glued geometry."""
    canvas = SchematicCanvas()
    glued = Device(
        "R1",
        "top",
        0.5,
        0.5,
        "Resistor_SMD",
        "R_0603",
        "assets/r1",
        "a" * 40,
        device_id="glued",
        pins=[ComponentPin("1", "1"), ComponentPin("2", "2")],
        schematic_x=9000,
        schematic_y=9000,
        schematic_rotation=90,
        schematic_glued=True,
    )
    free = replace(
        glued,
        device_id="free",
        reference="R2",
        schematic_glued=False,
    )
    canvas.set_project(ProjectDocument("Project", "Board", devices=[glued, free]))
    canvas.show()
    app.processEvents()
    canvas.optimize_layout()
    for _attempt in range(200):
        app.processEvents()
        if canvas._optimization_thread is None:
            break
        QTest.qWait(5)

    result = {device.device_id: device for device in canvas._project.devices}
    assert (result["glued"].schematic_x, result["glued"].schematic_y) == (9000, 9000)
    assert result["glued"].schematic_rotation == 90
    assert result["free"].schematic_glued is False
    assert result["free"].schematic_rotation % 90 == 0


def test_schematic_optimizer_recompacts_only_free_independent_pads(
    app: QApplication,
) -> None:
    """Final optimization translates free PADs but preserves glued PADs."""
    del app
    free = Pad(
        "P1",
        "top",
        0.1,
        0.1,
        "free-pad",
        schematic_x=2000.0,
        schematic_y=3000.0,
    )
    glued = replace(
        free,
        name="P2",
        pad_id="glued-pad",
        schematic_glued=True,
    )
    canvas = SchematicCanvas()
    canvas._project = ProjectDocument("Project", "Board", pads=[free, glued])

    canvas._apply_optimized_layout({}, {})

    result = {pad.pad_id: pad for pad in canvas._project.pads}
    assert result["free-pad"].schematic_x is None
    assert result["free-pad"].schematic_y is None
    assert result["glued-pad"].schematic_x == 2000.0
    assert result["glued-pad"].schematic_y == 3000.0


def test_schematic_spring_pass_shortens_visible_links() -> None:
    """Spring refinement pulls a long visible connection toward target length."""
    first = Device(
        "R1",
        "top",
        0.1,
        0.1,
        "Resistor",
        "R",
        "assets/r1",
        "revision",
        device_id="r1",
        pins=[ComponentPin("1", "1", net_id="SIG")],
    )
    second = replace(first, reference="R2", device_id="r2")
    positions = {"r1": QPointF(1000, 1000), "r2": QPointF(9000, 1000)}
    rotations = {"r1": 0.0, "r2": 0.0}
    before = QLineF(positions["r1"], positions["r2"]).length()

    SchematicLayoutOptimizer._spring_relax(
        [first, second],
        positions,
        rotations,
        {("r1", "r2")},
        20_000.0,
        SchematicCanvas._symbol_size,
        lambda: False,
    )

    after = QLineF(positions["r1"], positions["r2"]).length()
    assert after < before / 2


def test_schematic_spring_packs_connected_passives_without_overlap() -> None:
    """Connected resistors and capacitors settle at compact safe spacing."""
    resistor = Device(
        "R1",
        "top",
        0.1,
        0.1,
        "Resistor",
        "R",
        "assets/r1",
        "revision",
        device_id="r1",
        pins=[ComponentPin("1", "1", net_id="SIG")],
    )
    capacitor = replace(resistor, reference="C1", device_id="c1")
    positions = {"r1": QPointF(1000, 1000), "c1": QPointF(3000, 1000)}
    rotations = {"r1": 0.0, "c1": 0.0}

    SchematicLayoutOptimizer._spring_relax(
        [resistor, capacitor],
        positions,
        rotations,
        {("r1", "c1")},
        20_000.0,
        SchematicCanvas._symbol_size,
        lambda: False,
    )

    distance = QLineF(positions["r1"], positions["c1"]).length()
    assert distance <= 180.0
    assert not SchematicCanvas._device_bounds(resistor, positions["r1"]).intersects(
        SchematicCanvas._device_bounds(capacitor, positions["c1"])
    )


def test_schematic_spring_packs_parallel_passive_branches() -> None:
    """Passive branches sharing identical neighbors form a compact stack."""
    resistor = Device(
        "R1",
        "top",
        0.1,
        0.1,
        "Resistor",
        "R",
        "assets/r1",
        "revision",
        device_id="r1",
        pins=[ComponentPin("1", "1"), ComponentPin("2", "2")],
    )
    capacitor = replace(resistor, reference="C1", device_id="c1")
    left_ic = replace(
        resistor,
        reference="IC1",
        device_id="ic1",
        schematic_glued=True,
    )
    right_ic = replace(
        left_ic,
        reference="IC2",
        device_id="ic2",
    )
    devices = [left_ic, right_ic, resistor, capacitor]
    positions = {
        "ic1": QPointF(1000, 2000),
        "ic2": QPointF(3000, 2000),
        "r1": QPointF(2000, 1000),
        "c1": QPointF(2000, 3000),
    }
    rotations = {device.device_id: 0.0 for device in devices}
    edges = {
        ("ic1", "r1"),
        ("ic2", "r1"),
        ("ic1", "c1"),
        ("ic2", "c1"),
    }

    SchematicLayoutOptimizer._spring_relax(
        devices,
        positions,
        rotations,
        edges,
        20_000.0,
        SchematicCanvas._symbol_size,
        lambda: False,
    )

    distance = QLineF(positions["r1"], positions["c1"]).length()
    assert distance <= 120.0
    assert not SchematicCanvas._device_bounds(resistor, positions["r1"]).intersects(
        SchematicCanvas._device_bounds(capacitor, positions["c1"])
    )


def test_schematic_optimizer_aligns_right_passive_pin_to_ic() -> None:
    """Right-facing passive terminal wins alignment and forms a straight wire."""
    resistor = Device(
        "R3",
        "top",
        0.1,
        0.1,
        "Resistor",
        "R",
        "assets/r3",
        "revision",
        device_id="r3",
        pins=[ComponentPin("1", "1"), ComponentPin("2", "2")],
    )
    left_ic = Device(
        "IC1",
        "top",
        0.1,
        0.1,
        "Package_QFP",
        "IC",
        "assets/ic1",
        "revision",
        device_id="ic1",
        pins=[ComponentPin(str(index), str(index)) for index in range(1, 9)],
        schematic_glued=True,
    )
    right_ic = replace(left_ic, reference="IC5", device_id="ic5")
    devices = [left_ic, right_ic, resistor]
    positions = {
        "ic1": QPointF(1000, 1800),
        "ic5": QPointF(3000, 2000),
        "r3": QPointF(2000, 1500),
    }
    rotations = {device.device_id: 0.0 for device in devices}

    SchematicLayoutOptimizer._align_passive_terminals(
        devices,
        positions,
        rotations,
        [("ic1", 0, "r3", 0), ("ic5", 0, "r3", 1)],
        20_000.0,
        SchematicCanvas._symbol_size,
    )

    passive_pin = SchematicLayoutOptimizer._terminal_point(
        resistor,
        1,
        positions,
        rotations,
        SchematicCanvas._symbol_size,
    )
    ic_pin = SchematicLayoutOptimizer._terminal_point(
        right_ic,
        0,
        positions,
        rotations,
        SchematicCanvas._symbol_size,
    )
    assert passive_pin.y() == ic_pin.y()
    assert SchematicLayoutOptimizer._candidate_rotations(right_ic, 0.0) == (0.0,)


def test_schematic_final_translation_compacts_without_overlap() -> None:
    """Final translation pass reduces span without rotation or intersection."""
    first = Device(
        "R1",
        "top",
        0.1,
        0.1,
        "Resistor",
        "R",
        "assets/r1",
        "revision",
        device_id="r1",
        pins=[ComponentPin("1", "1"), ComponentPin("2", "2")],
    )
    second = replace(first, reference="C1", device_id="c1")
    third = replace(first, reference="R2", device_id="r2")
    devices = [first, second, third]
    positions = {
        "r1": QPointF(2000, 2000),
        "c1": QPointF(6000, 2000),
        "r2": QPointF(10_000, 2000),
    }
    rotations = {"r1": 0.0, "c1": 90.0, "r2": 180.0}
    original_rotations = dict(rotations)
    before = max(point.x() for point in positions.values()) - min(
        point.x() for point in positions.values()
    )

    SchematicLayoutOptimizer._compact_translations(
        devices,
        positions,
        rotations,
        set(),
        20_000.0,
        SchematicCanvas._symbol_size,
        lambda: False,
    )

    after = max(point.x() for point in positions.values()) - min(
        point.x() for point in positions.values()
    )
    bounds = [
        SchematicCanvas._device_bounds(device, positions[device.device_id])
        for device in devices
    ]
    assert after < before / 2
    assert rotations == original_rotations
    assert not any(
        left.intersects(right)
        for index, left in enumerate(bounds)
        for right in bounds[index + 1 :]
    )


def test_schematic_optimizer_button_is_schematic_only(window: MainWindow) -> None:
    """The expensive optimizer control is hidden outside Schematic."""
    window.show()
    QApplication.processEvents()
    schematic_index = window._tabs.indexOf(window._schematic_view)
    window._tabs.setCurrentIndex(0)
    assert not window._optimize_schematic_button.isVisible()
    window._tabs.setCurrentIndex(schematic_index)
    assert window._optimize_schematic_button.isVisible()


def test_schematic_drag_records_one_history_state_on_release(
    window: MainWindow, tmp_path: Path
) -> None:
    """Intermediate schematic drag positions do not create undo entries."""
    window.store = ProjectStore(tmp_path / "drag.revp")
    device = Device(
        "R1",
        "top",
        0.5,
        0.5,
        "Resistor_SMD",
        "R_0603",
        "assets/r1",
        "a" * 40,
        device_id="dragged",
        schematic_x=9000,
        schematic_y=9000,
    )
    window.project = ProjectDocument("Project", "Board", devices=[device])
    window._reset_history()
    initial_length = len(window._history)

    window._schematic_layout_started("dragged")
    for position in (9100, 9200, 9300):
        window.project.devices[0] = replace(
            window.project.devices[0], schematic_x=position
        )
        window._schematic_layout_changed("dragged", position, 9000)
    assert len(window._history) == initial_length

    window._schematic_layout_finished("dragged")
    assert len(window._history) == initial_length + 1


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


def test_conventional_diode_crystal_and_transistor_references(
    window: MainWindow, tmp_path: Path
) -> None:
    """Use conventional prefixes and skip references already in the project."""
    window.project = ProjectDocument(
        "Project",
        "Board",
        devices=[
            Device(
                "D1",
                "top",
                0.5,
                0.5,
                "Diode_SMD",
                "D_0603",
                "assets/kicad/d1.kicad_mod",
                "a" * 40,
            )
        ],
    )
    diode = FootprintReference("Diode_SMD", "D_0603", tmp_path / "d.kicad_mod")
    crystal = FootprintReference("Crystal_SMD", "Crystal", tmp_path / "y.kicad_mod")
    transistor = FootprintReference(
        "Transistor_SMD", "SOT-23", tmp_path / "q.kicad_mod"
    )

    assert window._next_device_reference(diode) == "D2"
    assert window._next_device_reference(crystal) == "Y1"
    assert window._next_device_reference(transistor) == "Q1"


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
