"""Image import, calibration, crop, and editing GUI tests."""

# Qt uses compiled extension modules; test fixtures intentionally inspect GUI
# state to verify lifecycle behavior.
# pylint: disable=wrong-import-position,no-name-in-module,redefined-outer-name
# pylint: disable=unused-argument,protected-access,unused-import,duplicate-code

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
        "tnasrevner.lib.image_import.QFileDialog.getOpenFileName",
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
    assert window.project.images[0].path == "assets/original/top.png"
    assert window.store.read_asset("assets/original/top.png").startswith(b"\x89PNG")
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


def test_import_editor_rotation_moves_crop_with_image(app: QApplication) -> None:
    """A crop rectangle follows image rotation instead of keeping old ratios."""
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image))
    dialog._set_selection(QPoint(10, 10), QPoint(80, 60))
    before = dialog._source_rect(dialog._selection)

    dialog._rotate(90)

    rotated = dialog._source_rect(dialog._selection)
    assert rotated.x() == pytest.approx(
        image.height() - before.y() - before.height(), abs=1
    )
    assert rotated.y() == pytest.approx(before.x(), abs=1)
    assert rotated.width() == pytest.approx(before.height(), abs=1)
    assert rotated.height() == pytest.approx(before.width(), abs=1)

    dialog._rotate(-90)

    restored = dialog._source_rect(dialog._selection)
    assert restored.x() == pytest.approx(before.x(), abs=1)
    assert restored.y() == pytest.approx(before.y(), abs=1)
    assert restored.width() == pytest.approx(before.width(), abs=1)
    assert restored.height() == pytest.approx(before.height(), abs=1)
    dialog.close()


def test_import_editor_free_rotation_does_not_expand_crop_repeatedly(
    app: QApplication,
) -> None:
    """Returning to a free angle restores the same transformed crop bounds."""
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image))
    dialog._set_selection(QPoint(10, 10), QPoint(80, 60))

    dialog._set_angle(30)
    first = dialog._source_rect(dialog._selection)
    dialog._set_angle(45)
    dialog._set_angle(30)
    repeated = dialog._source_rect(dialog._selection)

    assert repeated.x() == pytest.approx(first.x(), abs=1)
    assert repeated.y() == pytest.approx(first.y(), abs=1)
    assert repeated.width() == pytest.approx(first.width(), abs=1)
    assert repeated.height() == pytest.approx(first.height(), abs=1)
    dialog.close()


def test_bottom_editor_shows_right_edge_flip_orientation_guide(
    app: QApplication,
) -> None:
    """Bottom calibration explains the fixed Top-to-Bottom board orientation."""
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image), side="bottom")

    assert not dialog._orientation_guide.pixmap().isNull()
    assert "right edge" in dialog._orientation_guide.toolTip().lower()
    dialog.close()


def test_import_editor_shows_top_and_bottom_photos_together(
    app: QApplication,
) -> None:
    """Top + Bottom displays the opposite board photo beside the active one."""
    top = QImage(200, 100, QImage.Format.Format_RGB32)
    top.fill(QColor("red"))
    bottom = QImage(100, 200, QImage.Format.Format_RGB32)
    bottom.fill(QColor("blue"))
    dialog = ImageEditDialog(
        QPixmap.fromImage(top),
        side="top",
        comparison_image=QPixmap.fromImage(bottom),
    )
    dialog.show()
    app.processEvents()

    dialog._both_sides_button.click()
    app.processEvents()

    assert dialog._comparison_panel.isVisible()
    assert dialog._both_sides_button.isChecked()
    assert not dialog._comparison_canvas.pixmap().isNull()
    assert dialog._current_side_label.text() == "Top"
    assert dialog._comparison_side_label.text() == "Bottom"
    dialog.close()


def test_import_editor_switches_to_other_side_after_valid_setup(
    app: QApplication,
) -> None:
    """Selecting the other face accepts valid work and requests that face."""
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(QPixmap.fromImage(image), side="top")
    dialog._set_selection(QPoint(0, 0), QPoint(199, 99))
    dialog._set_calibration_line(QPoint(10, 10), QPoint(110, 10))
    dialog._millimeters.setValue(20)

    dialog._bottom_side_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.requested_side() == "bottom"
    dialog.close()


def test_overlay_mirrors_bottom_pad_position_and_rotation(
    window: MainWindow,
) -> None:
    """Both view applies the right-edge flip to Bottom pad geometry."""
    bottom = Pad(
        "P1",
        "bottom",
        0.1,
        0.2,
        "bottom-pad",
        0.2,
        0.1,
        rotation=30.0,
    )
    window.project = ProjectDocument("Project", "Board", pads=[bottom])
    window._pad_display_mode = "both"

    labels = window._overlay_pad_labels()

    assert len(labels) == 1
    assert labels[0].x == pytest.approx(0.7)
    assert labels[0].y == pytest.approx(0.2)
    assert labels[0].rotation == pytest.approx(330.0)


def test_import_editor_can_return_to_uncropped_original(app: QApplication) -> None:
    """Original button restores the raw image without closing the editor."""
    original = QImage(240, 160, QImage.Format.Format_RGB32)
    original.fill(0x123456)
    working = original.copy(20, 20, 80, 60)
    dialog = ImageEditDialog(
        QPixmap.fromImage(working), original_image=QPixmap.fromImage(original)
    )
    dialog.show()
    app.processEvents()

    dialog._original_button.click()
    app.processEvents()

    assert dialog._source.size() == original.size()
    assert dialog.isVisible()
    assert dialog.started_from_original()
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


def test_rotate_board_does_not_overwrite_original_image(
    window: MainWindow, tmp_path: Path
) -> None:
    """Board rotation records an operation instead of destroying the source."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    image = QImage(80, 40, QImage.Format.Format_RGB32)
    image.fill(0x123456)
    source = window._pixmap_bytes(QPixmap.fromImage(image))
    source_path = "assets/original/top.png"
    window.store.write_asset(source_path, source)
    window.project.images.append(
        ImageAsset(
            "top",
            source_path,
            "top.png",
            original_path=source_path,
        )
    )
    window._refresh_views()

    window._rotate_board_90()

    assert window.store.read_asset(source_path) == source
    assert window.project.images[0].transformations == ((90.0, (0.0, 0.0, 1.0, 1.0)),)


def test_edit_image_rotation_rotates_existing_side_geometry(
    window: MainWindow,
) -> None:
    """Photo quarter-turns rotate pads and footprints only on edited side."""
    top_pad = Pad("P1", "top", 0.2, 0.3, "pad-id", 0.1, 0.2, rotation=15.0)
    bottom_pad = Pad("P2", "bottom", 0.2, 0.3, "other-pad", 0.1, 0.2)
    top_device = Device(
        "R1",
        "top",
        0.2,
        0.3,
        "Resistor_SMD",
        "R_0603",
        "assets/kicad/r1.kicad_mod",
        "a" * 40,
    )
    window.project = ProjectDocument(
        "Project", "Board", pads=[top_pad, bottom_pad], devices=[top_device]
    )

    window._rotate_image_side_geometry("top", 90.0)

    assert window.project.pads[0].x == pytest.approx(0.5)
    assert window.project.pads[0].y == pytest.approx(0.2)
    assert window.project.pads[0].width == pytest.approx(0.2)
    assert window.project.pads[0].height == pytest.approx(0.1)
    assert window.project.pads[0].rotation == pytest.approx(105.0)
    assert window.project.devices[0].x == pytest.approx(0.7)
    assert window.project.devices[0].y == pytest.approx(0.2)
    assert window.project.devices[0].rotation == pytest.approx(90.0)
    assert window.project.pads[1] == bottom_pad

    window._rotate_image_side_geometry("top", -90.0)

    assert window.project.pads[0].x == pytest.approx(top_pad.x)
    assert window.project.pads[0].y == pytest.approx(top_pad.y)
    assert window.project.pads[0].rotation == pytest.approx(top_pad.rotation)
    assert window.project.devices[0].x == pytest.approx(top_device.x)
    assert window.project.devices[0].y == pytest.approx(top_device.y)
    assert window.project.devices[0].rotation == pytest.approx(top_device.rotation)


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


def test_canceling_footprint_selection_keeps_image_editor_open(
    app: QApplication,
) -> None:
    """Canceling footprint calibration leaves the image editor open."""
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog = ImageEditDialog(
        QPixmap.fromImage(image), footprint_selector=lambda _: None
    )
    dialog.show()
    app.processEvents()

    dialog._footprint_button.click()
    app.processEvents()

    assert dialog.isVisible()
    assert dialog._calibration_method == "line"
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
