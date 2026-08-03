"""Functional tests for minimal project lifecycle GUI actions."""

# Qt uses compiled extension modules; test fixtures intentionally inspect GUI
# state to verify lifecycle behavior.
# pylint: disable=wrong-import-position,no-name-in-module,redefined-outer-name
# pylint: disable=unused-argument,protected-access,too-many-lines

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QColor, QImage, QPixmap, QValidator, QWheelEvent
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
    window._tabs.setCurrentIndex(5)

    assert window.save_project()
    assert window.project.display.mode == "bom"

    window._tabs.setCurrentIndex(0)
    window._refresh_views()
    assert window._tabs.currentIndex() == 5


def test_save_and_restore_schematic_view(window: MainWindow, tmp_path: Path) -> None:
    """The Schematic tab is a persisted project display mode."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    window._tabs.setCurrentIndex(6)

    assert window.save_project()
    assert window.project.display.mode == "schematic"

    window._tabs.setCurrentIndex(0)
    window._refresh_views()
    assert window._tabs.currentIndex() == 6


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
        action.toolTip().startswith("Create a pad")
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
        button.toolTip().startswith("Select and place")
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
    monkeypatch.setattr("tnasrevner.gui.FootprintPickerDialog", FakePicker)

    def reference_popup(*_args, **_kwargs) -> tuple[str, bool]:
        order.append("reference")
        return "R1", True

    monkeypatch.setattr("tnasrevner.gui.QInputDialog.getText", reference_popup)

    window.add_device()

    assert order == ["footprint", "reference"]
    assert window._pending_device is not None
    assert window._pending_device.reference == "R1"


def test_tools_palette_buttons_have_icons_and_hover_help(window: MainWindow) -> None:
    """Every tool action exposes an icon and mouse-over description."""
    buttons = window._tools_dock.widget().findChildren(QPushButton)
    assert buttons
    assert all(not button.icon().isNull() for button in buttons)
    assert all(button.toolTip() for button in buttons)


def test_kicad_cache_failure_cancels_pending_import(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first-run cache failure prevents the pending device workflow."""
    warnings: list[str] = []
    window._add_device_pending = True
    monkeypatch.setattr(
        "tnasrevner.gui.QMessageBox.warning",
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
        "tnasrevner.gui.QMessageBox.warning",
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

    monkeypatch.setattr("tnasrevner.gui.QInputDialog.getText", reference_popup)

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


def test_unknown_device_reference_uses_u_prefix(
    window: MainWindow, tmp_path: Path
) -> None:
    """Unknown KiCad footprints use the standard U1, U2 references."""
    window.project = ProjectDocument("Project", "Board")
    source = FootprintReference(
        "Custom_Logic", "Mystery", tmp_path / "mystery.kicad_mod"
    )

    assert window._next_device_reference(source) == "U1"
    window.project.devices.append(
        Device(
            "U1",
            "top",
            0.5,
            0.5,
            source.library,
            source.name,
            "assets/kicad/u1.kicad_mod",
            "a" * 40,
        )
    )
    assert window._next_device_reference(source) == "U2"


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
        "tnasrevner.gui.QMessageBox.information",
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
    assert not window._net_table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable
    assert window._net_table.item(0, 1).flags() & Qt.ItemFlag.ItemIsEditable
    assert not window._net_table.item(0, 2).flags() & Qt.ItemFlag.ItemIsEditable
    assert window._net_table.item(0, 3).flags() & Qt.ItemFlag.ItemIsEditable
    assert not window._net_table.item(0, 4).flags() & Qt.ItemFlag.ItemIsEditable

    window._connect_pad_to_net("top", 0.15, 0.15)
    assert window._net_dialog is not None
    window._net_dialog.reject()
    QApplication.processEvents()
    assert window._net_dialog is None


def test_disconnect_other_pad_keeps_active_link_selection(window: MainWindow) -> None:
    """Disconnecting a non-origin pad keeps origin NET visualization active."""
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad("P1", "top", 0.1, 0.1, "one", 0.1, 0.1, "GND"),
            Pad("P2", "top", 0.5, 0.5, "two", 0.1, 0.1, "GND"),
        ],
    )
    window._selected_net = "GND"
    window._selected_pad_id = "one"

    window._assign_pad_net("two", "")

    assert window.project.pads[0].net == "GND"
    assert window.project.pads[1].net is None
    assert window._selected_net == "GND"
    assert window._selected_pad_id == "one"


def test_shift_selected_terminals_create_generic_net(window: MainWindow) -> None:
    """Two schematic terminals without nets receive the next automatic net."""
    device = Device(
        "U1",
        "top",
        0.5,
        0.5,
        "Package_QFP",
        "QFP",
        "assets/kicad/u1.kicad_mod",
        "a" * 40,
        device_id="device",
        pins=[ComponentPin("1", "IO", footprint_pad="1")],
    )
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad("U1.1", "top", 0.1, 0.1, "generated", device_id="device", number="1"),
            Pad("P1", "top", 0.3, 0.3, "standalone"),
        ],
        devices=[device],
    )

    window._connect_schematic_terminals(("pin", "device", "1"), ("pin", "device", "1"))
    assert window.project.devices[0].pins[0].net_id == "N1"
    assert window.project.pads[1].net is None

    window._connect_schematic_terminals(
        ("pin", "device", "1"), ("pad", "standalone", None)
    )
    assert {pad.net for pad in window.project.pads} == {"N1"}


def test_shift_selected_board_pads_connect_immediately(
    window: MainWindow, tmp_path: Path
) -> None:
    """Shift-click links a target to the selected origin before Escape."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad("P1", "top", 0.1, 0.1, "one", 0.1, 0.1),
            Pad("P2", "top", 0.5, 0.5, "two", 0.1, 0.1),
        ],
    )
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0x202020)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.project.images.append(ImageAsset("top", "assets/top.png", "top.png"))
    window._refresh_views()
    window.show()
    QApplication.processEvents()
    view = window._views["top"]

    origin = QPoint(
        round(0.15 * (view._label.width() - 1)),
        round(0.15 * (view._label.height() - 1)),
    )
    QTest.mouseClick(view._label, Qt.MouseButton.LeftButton, pos=origin)
    QApplication.processEvents()
    assert window._selected_pad_id == "one"

    target = QPoint(
        round(0.55 * (view._label.width() - 1)),
        round(0.55 * (view._label.height() - 1)),
    )
    QTest.mouseClick(
        view._label,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ShiftModifier,
        target,
    )
    QApplication.processEvents()
    assert window._pending_board_connection_pads == ["one", "two"]
    assert [pad.net for pad in window.project.pads] == ["N1", "N1"]
    assert window._selected_net == "N1"
    assert window._net_mode_label.isVisible()
    assert view.cursor().shape() == Qt.CursorShape.CrossCursor
    link_color = view._label.pixmap().toImage().pixelColor(35, 35)
    assert min(link_color.red(), link_color.green(), link_color.blue()) > 200

    trace_point = QPoint(
        round(0.35 * (view._label.width() - 1)),
        round(0.35 * (view._label.height() - 1)),
    )
    QTest.mouseClick(view._label, Qt.MouseButton.RightButton, pos=trace_point)
    QApplication.processEvents()
    assert window._pad_menu is not None
    assert [action.text() for action in window._pad_menu.actions()] == ["Disconnect"]
    window._pad_menu.actions()[0].trigger()
    QApplication.processEvents()
    assert [pad.net for pad in window.project.pads] == ["N1", None]
    assert window._selected_net == "N1"
    assert window._selected_pad_id == "one"

    QTest.keyClick(window, Qt.Key.Key_Escape)

    assert [pad.net for pad in window.project.pads] == ["N1", None]
    assert not window._pending_board_connection_pads
    assert not window._net_mode_label.isVisible()
    assert view.cursor().shape() != Qt.CursorShape.CrossCursor


def test_net_table_enter_keeps_nets_tab(window: MainWindow) -> None:
    """Committing Net or Function edits must not jump back to Top."""
    device = Device(
        "U1",
        "top",
        0.5,
        0.5,
        "Package_QFP",
        "QFP",
        "assets/kicad/u1.kicad_mod",
        "a" * 40,
        device_id="device",
        pins=[ComponentPin("1", "IO", footprint_pad="1")],
    )
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad("U1.1", "top", 0.1, 0.1, "generated", device_id="device", number="1")
        ],
        devices=[device],
    )
    window._refresh_net_table()
    window._tabs.setCurrentIndex(4)

    window._net_table.item(0, 1).setText("N1")
    QApplication.processEvents()
    assert window._tabs.currentIndex() == 4

    window._net_table.item(0, 3).setText("GPIO")
    QApplication.processEvents()
    assert window._tabs.currentIndex() == 4


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


def test_shift_click_device_edits_bom_value_and_deletes_whole_footprint(
    window: MainWindow, tmp_path: Path
) -> None:
    """Generated pads expose device actions that update BOM or remove everything."""
    window.store = ProjectStore(tmp_path / "board.revp")
    device = Device(
        "C1",
        "top",
        0.5,
        0.5,
        "Capacitor_SMD",
        "C_0603",
        "assets/kicad/device.kicad_mod",
        "a" * 40,
        device_id="device-id",
    )
    pad = Pad(
        "C1.1",
        "top",
        0.1,
        0.1,
        "pad-id",
        0.1,
        0.1,
        device_id=device.device_id,
        number="1",
    )
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.project = ProjectDocument(
        "Project",
        "Board",
        images=[
            ImageAsset(
                "top",
                "assets/top.png",
                "top.png",
                pixels_per_mm=10,
                calibration_line=(0.0, 0.5, 1.0, 0.5),
                calibration_length_mm=10,
            )
        ],
        pads=[pad],
        devices=[device],
    )
    window.store.write_asset(device.footprint_path, FOOTPRINT)

    window._show_pad_menu("top", 0.5, 0.5)
    assert window._pad_menu is not None
    actions = {action.text(): action for action in window._pad_menu.actions()}
    assert set(actions) == {"Delete device", "Set value…"}
    actions["Set value…"].trigger()
    assert window._device_value_dialog is not None
    window._device_value_dialog.setTextValue("100 nF")
    window._device_value_dialog.accept()
    QApplication.processEvents()

    assert window.project.devices[0].value == "100 nF"
    assert window._bom_table.item(0, 0).text() == "C1"
    assert window._bom_table.item(0, 1).text() == "Capacitor"
    assert window._bom_table.item(0, 2).text() == "C_0603"
    assert window._bom_table.item(0, 3).text() == "100 nF"
    if window._pad_menu is not None:
        window._pad_menu.close()

    window._show_pad_menu("top", 0.5, 0.5)
    assert window._pad_menu is not None
    actions = {action.text(): action for action in window._pad_menu.actions()}
    actions["Delete device"].trigger()

    assert not window.project.devices
    assert not window.project.pads
    with pytest.raises(ProjectFormatError):
        window.store.read_asset(device.footprint_path)
    if window._pad_menu is not None:
        window._pad_menu.close()


def test_bom_value_and_object_dropdown_are_editable(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BOM edits values and creates reusable object types through NEW."""
    window.project = ProjectDocument(
        "Project",
        "Board",
        devices=[
            Device(
                "C1",
                "top",
                0.5,
                0.5,
                "Capacitor_SMD",
                "C_0603",
                "assets/kicad/c1.kicad_mod",
                "a" * 40,
            )
        ],
    )
    window._refresh_bom_table()

    assert not window._bom_table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable
    assert not window._bom_table.item(0, 1).flags() & Qt.ItemFlag.ItemIsEditable
    assert window._bom_table.item(0, 3).flags() & Qt.ItemFlag.ItemIsEditable
    assert not window._bom_table.item(0, 4).flags() & Qt.ItemFlag.ItemIsEditable
    assert window._bom_table.item(0, 5).flags() & Qt.ItemFlag.ItemIsEditable
    window._bom_table.item(0, 3).setText("100 nF")
    assert window.project.devices[0].value == "100 nF"
    window._bom_table.item(0, 5).setText("https://example.test/datasheet")
    assert window.project.devices[0].datasheet == "https://example.test/datasheet"

    monkeypatch.setattr(
        "tnasrevner.gui.QInputDialog.getText",
        lambda *_args, **_kwargs: ("Sensor", True),
    )
    combo = window._bom_table.cellWidget(0, 1)
    combo.setCurrentText("NEW")
    QApplication.processEvents()
    assert window.project.devices[0].object_type == "Sensor"


def test_pad_mouse_rectangle_keeps_continuous_placement_mode(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real drag keeps placement armed for the next pad."""
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
    assert window._views["top"]._pad_placement
    assert window._pending_pad is not None
    assert window._pending_pad.name == "P2"

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
        lambda image, **_kwargs: (image, 10.0, (0.1, 0.2, 0.8, 0.2), 7.0),
    )

    window.import_picture()

    assert window.project.images[0].side == "top"
    assert window.store.read_asset("assets/top.png").startswith(b"\x89PNG")
    original_path = window.project.images[0].original_path
    assert original_path == "assets/original/top.png"
    assert window.store.read_asset(original_path).startswith(b"\x89PNG")
    assert window.project.images[0].calibration_line == (0.1, 0.2, 0.8, 0.2)
    assert window.project.images[0].calibration_length_mm == 7.0


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


def test_import_measurement_accepts_dot_and_comma_decimals(app: QApplication) -> None:
    """macOS locale must not turn 30.5 mm into 305 mm."""
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image))

    assert dialog._millimeters.valueFromText("30.5 mm") == pytest.approx(30.5)
    assert dialog._millimeters.valueFromText("30,5 mm") == pytest.approx(30.5)
    assert dialog._millimeters.validate("30,5 mm", 4)[0] == QValidator.State.Acceptable
    dialog.close()


def test_editing_existing_image_preserves_physical_scale(app: QApplication) -> None:
    """FIT display scaling must not alter persisted source pixels per mm."""
    image = QImage(2000, 1000, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image))
    dialog.resize(800, 600)
    dialog._render()
    assert dialog._display_scale < 1

    dialog.prepare_existing_image(25.0, (0.1, 0.5, 0.6, 0.5), 40.0)

    assert dialog.pixels_per_mm() == pytest.approx(25.0, rel=0.01)
    assert dialog.calibration_length_mm() == 40.0
    dialog.close()


def test_scaling_footprint_does_not_recrop_existing_image(
    app: QApplication,
) -> None:
    """Editing footprint scale preserves existing image dimensions."""
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image))
    dialog.resize(800, 600)
    dialog._render()
    dialog.prepare_existing_image(25.0, (0.1, 0.5, 0.6, 0.5), 40.0)
    dialog._calibration_footprint = parse_footprint(FOOTPRINT, "Resistor_SMD")
    dialog._footprint_center = QPointF(100, 50)
    dialog._footprint_pixels_per_mm = 10.0
    dialog._set_edit_mode("footprint")
    dialog._render()
    dialog.accept()

    assert dialog.result_pixmap().size() == image.size()
    dialog.close()


def test_dragging_footprint_scale_preserves_existing_crop(
    app: QApplication,
) -> None:
    """A real footprint-handle drag must not alter the existing crop."""
    image = QImage(1000, 1000, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image))
    dialog.resize(700, 500)
    dialog._render()
    dialog.prepare_existing_image(20.0, (0.1, 0.5, 0.6, 0.5), 25.0)
    dialog._calibration_footprint = parse_footprint(FOOTPRINT, "Resistor_SMD")
    dialog._footprint_center = QPointF(500, 500)
    dialog._footprint_pixels_per_mm = 20.0
    dialog._set_edit_mode("footprint")
    dialog._render()
    app.processEvents()

    radius = dialog._calibration_footprint.radius() * 20.0
    handle = QPoint(
        round((500 + radius) * dialog._display_scale),
        round((500 + radius) * dialog._display_scale),
    )
    QTest.mousePress(dialog._canvas, Qt.MouseButton.LeftButton, pos=handle)
    QTest.mouseMove(dialog._canvas, pos=handle + QPoint(20, 20))
    QTest.mouseRelease(
        dialog._canvas, Qt.MouseButton.LeftButton, pos=handle + QPoint(20, 20)
    )
    dialog.accept()

    assert dialog.result_pixmap().size() == image.size()
    dialog.close()


def test_footprint_handle_is_rendered_at_display_scale_and_scales(
    app: QApplication,
) -> None:
    """The visible corner handle matches hit-testing at fitted image scale."""
    image = QImage(1000, 1000, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image))
    dialog.resize(700, 500)
    dialog._calibration_footprint = parse_footprint(FOOTPRINT, "Resistor_SMD")
    dialog._footprint_center = QPointF(500, 500)
    dialog._footprint_pixels_per_mm = 20.0
    dialog._set_edit_mode("footprint")
    dialog._render()
    app.processEvents()

    radius = dialog._calibration_footprint.radius() * 20.0
    expected = QPoint(
        round((500 + radius) * dialog._display_scale),
        round((500 + radius) * dialog._display_scale),
    )
    rendered = dialog._canvas.pixmap().toImage()
    yellow_pixels = [
        QColor(rendered.pixel(x, y))
        for x in range(
            max(0, expected.x() - 4), min(rendered.width(), expected.x() + 5)
        )
        for y in range(
            max(0, expected.y() - 4), min(rendered.height(), expected.y() + 5)
        )
    ]
    assert any(
        color.red() > 200 and color.green() > 200 and color.blue() < 100
        for color in yellow_pixels
    )

    QTest.mousePress(dialog._canvas, Qt.MouseButton.LeftButton, pos=expected)
    assert dialog._footprint_drag_mode == "scale"
    old_scale = dialog._footprint_pixels_per_mm
    old_top_left = QPointF(500 - radius, 500 - radius)
    QTest.mouseMove(dialog._canvas, expected + QPoint(20, 20))
    assert dialog._footprint_pixels_per_mm > old_scale
    new_radius = (
        dialog._calibration_footprint.radius() * dialog._footprint_pixels_per_mm
    )
    assert dialog._footprint_center.x() - new_radius == pytest.approx(old_top_left.x())
    assert dialog._footprint_center.y() - new_radius == pytest.approx(old_top_left.y())
    assert new_radius > radius
    QTest.mouseRelease(dialog._canvas, Qt.MouseButton.LeftButton, pos=expected)
    dialog.close()


def test_footprint_handle_hit_area_stays_selectable_when_zoomed(
    app: QApplication,
) -> None:
    """The handle hit area remains screen-sized at high and low zoom."""
    image = QImage(1000, 1000, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image))
    dialog.resize(700, 500)
    dialog._calibration_footprint = parse_footprint(FOOTPRINT, "Resistor_SMD")
    dialog._footprint_center = QPointF(500, 500)
    dialog._footprint_pixels_per_mm = 10.0
    dialog._set_edit_mode("footprint")
    for zoom in (0.5, 2.0):
        dialog._zoom = zoom
        dialog._render()
        app.processEvents()
        radius = dialog._calibration_footprint.radius() * 10.0
        handle = QPoint(
            round((500 + radius) * dialog._display_scale),
            round((500 + radius) * dialog._display_scale),
        )
        dialog._footprint_drag_mode = None
        QTest.mousePress(dialog._canvas, Qt.MouseButton.LeftButton, pos=handle)
        assert dialog._footprint_drag_mode == "scale"
        QTest.mouseRelease(dialog._canvas, Qt.MouseButton.LeftButton, pos=handle)
    dialog.close()


def test_footprint_drag_moves_only_inside_footprint(
    app: QApplication,
) -> None:
    """Dragging footprint body moves it; clicking outside does nothing."""
    image = QImage(1000, 1000, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image))
    dialog._calibration_footprint = parse_footprint(FOOTPRINT, "Resistor_SMD")
    dialog._footprint_center = QPointF(500, 500)
    dialog._footprint_pixels_per_mm = 10.0
    dialog._set_edit_mode("footprint")
    dialog._render()
    app.processEvents()
    old_center = QPointF(dialog._footprint_center)

    center = QPoint(
        round(500 * dialog._display_scale), round(500 * dialog._display_scale)
    )
    QTest.mousePress(dialog._canvas, Qt.MouseButton.LeftButton, pos=center)
    assert dialog._footprint_drag_mode == "move"
    QTest.mouseMove(dialog._canvas, center + QPoint(10, 5))
    QTest.mouseRelease(
        dialog._canvas, Qt.MouseButton.LeftButton, pos=center + QPoint(10, 5)
    )
    assert dialog._footprint_center.x() > old_center.x()
    assert dialog._footprint_center.y() > old_center.y()

    outside = QPoint(10, 10)
    app.processEvents()
    QTest.mousePress(dialog._canvas, Qt.MouseButton.LeftButton, pos=outside)
    assert dialog._footprint_drag_mode is None
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
