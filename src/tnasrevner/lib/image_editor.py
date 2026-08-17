"""Imported-image crop, rotation, and calibration editor."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,invalid-name,unused-import,too-many-lines
# pylint: disable=too-many-locals,too-many-statements
# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

from collections.abc import Callable
import base64
from dataclasses import dataclass, replace
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import sys
from time import monotonic
from uuid import uuid4

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QEvent,
    QIODevice,
    QLineF,
    QLocale,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSettings,
    QStandardPaths,
    QThread,
    QTimer,
    Qt,
    Signal,
    Slot,
    qInstallMessageHandler,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCloseEvent,
    QCursor,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QMenu,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QPushButton,
    QProgressDialog,
    QRubberBand,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from ..kicad import (
    CacheResult,
    Footprint,
    FootprintReference,
    KiCadCacheError,
    KiCadFootprintCache,
    KiCadFormatError,
    place_footprint_pads,
    parse_footprint,
)
from ..project import (
    ComponentPin,
    Device,
    ImageAsset,
    Net,
    Pad,
    ProjectDocument,
    ProjectFormatError,
    ProjectStore,
)

# pylint: disable=unused-import

from .footprints import _paint_footprint
from .app_support import MeasurementSpinBox


class CropOverlay(QWidget):
    """Show the crop rectangle while dimming pixels outside it."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._selection = QRect()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_selection(self, selection: QRect | None) -> None:
        """Update crop bounds in canvas coordinates."""
        self._selection = selection or QRect()
        self.update()

    def paintEvent(  # pylint: disable=too-many-locals,too-many-statements
        self, event
    ) -> None:  # noqa: N802
        """Paint dimmed outside area and highlighted crop boundary."""
        del event
        if self._selection.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selection = self._selection.intersected(self.rect())
        shade = QColor(0, 0, 0, 125)
        painter.fillRect(0, 0, self.width(), selection.top(), shade)
        painter.fillRect(0, selection.bottom() + 1, self.width(), self.height(), shade)
        painter.fillRect(
            0, selection.top(), selection.left(), selection.height(), shade
        )
        painter.fillRect(
            selection.right() + 1,
            selection.top(),
            self.width(),
            selection.height(),
            shade,
        )
        painter.setPen(QPen(QColor(255, 212, 42), 2, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(selection).adjusted(1, 1, -1, -1))
        painter.end()


class ImageEditDialog(  # pylint: disable=too-many-instance-attributes,too-many-statements,too-many-return-statements,too-many-branches,too-many-locals
    QDialog
):
    """Rotate and crop an image before it enters a project archive."""

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments
    def __init__(
        self,
        image: QPixmap,
        parent: QWidget | None = None,
        footprint_selector: Callable[[QWidget], Footprint | None] | None = None,
        load_callback: Callable[[], None] | None = None,
        original_image: QPixmap | None = None,
        side: str | None = None,
        comparison_image: QPixmap | None = None,
    ) -> None:  # pylint: disable=too-many-locals,too-many-statements
        """Create an image calibration editor.

        Args:
            image: Working image shown in the editor.
            parent: Optional owning widget.
            footprint_selector: Callback used to select a calibration footprint.
            load_callback: Callback used to replace the image for this side.
            original_image: Optional uncropped source image.
            side: Board face being edited; Bottom shows the orientation guide.
            comparison_image: Optional image from the opposite board face.
        """
        super().__init__(parent)
        self.setWindowTitle("Edit imported image")
        self.resize(1000, 700)
        self._base_image = image
        self._source = image
        self._side = side
        self._requested_side: str | None = None
        self._comparison_image = (
            QPixmap(comparison_image) if comparison_image is not None else QPixmap()
        )
        self._showing_both_sides = False
        self._original_image = (
            QPixmap(original_image) if original_image is not None else QPixmap()
        )
        self._started_from_original = False
        self._angle = 0.0
        self._zoom = 1.0
        self._zoom_revision = 0
        self._display_scale = 1.0
        self._selection_start: QPoint | None = None
        self._pan_position: QPoint | None = None
        self._selection = None
        self._selection_source_ratio: tuple[float, float, float, float] | None = None
        self._selection_base_points: tuple[QPointF, ...] | None = None
        self._editing_existing_image = False
        self._crop_selection_modified = False
        self._resize_edges: set[str] = set()
        self._calibration_start: QPoint | None = None
        self._calibration_end: QPoint | None = None
        self._calibration_line: tuple[QPointF, QPointF] | None = None
        self._result_calibration_line: tuple[float, float, float, float] | None = None
        self._result_transformation: (
            tuple[float, tuple[float, float, float, float]] | None
        ) = None
        self._calibration_method = "line"
        self._footprint_selector = footprint_selector
        self._calibration_footprint: Footprint | None = None
        self._footprint_center: QPointF | None = None
        self._footprint_pixels_per_mm = 0.0
        self._footprint_rotation = 0.0
        self._footprint_drag_mode: str | None = None
        self._footprint_drag_offset = QPointF()
        self._footprint_scale_anchor: QPointF | None = None
        self._edit_mode = "calibration"
        self._canvas = QLabel()
        self._canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._canvas.installEventFilter(self)
        self._rubber_band = CropOverlay(self._canvas)
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._canvas)
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._comparison_canvas = QLabel()
        self._comparison_canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._comparison_scroll = QScrollArea()
        self._comparison_scroll.setWidget(self._comparison_canvas)
        self._comparison_scroll.setWidgetResizable(False)
        self._comparison_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        calibration_button = QPushButton("Scale line")
        calibration_button.setCheckable(True)
        calibration_button.setChecked(True)
        calibration_button.clicked.connect(lambda: self._set_edit_mode("calibration"))
        align_button = QPushButton("Align line")
        align_button.setToolTip("Draw a line and rotate the image horizontally")
        align_button.setCheckable(True)
        align_button.clicked.connect(lambda: self._set_edit_mode("align"))
        footprint_button = QPushButton("Scale footprint")
        footprint_button.setToolTip("Choose a KiCad footprint as a physical reference")
        footprint_button.setEnabled(footprint_selector is not None)
        footprint_button.clicked.connect(self._choose_footprint_calibration)
        crop_button = QPushButton("Crop rectangle")
        crop_button.setToolTip("Use Shift to draw border")
        crop_button.setCheckable(True)
        crop_button.clicked.connect(lambda: self._set_edit_mode("crop"))
        self._calibration_button = calibration_button
        self._align_button = align_button
        self._footprint_button = footprint_button
        self._crop_button = crop_button
        load_image_button = QPushButton("Import")
        load_image_button.setToolTip("Import another image for this side")
        load_image_button.setEnabled(load_callback is not None)
        if load_callback is not None:
            load_image_button.clicked.connect(
                lambda: self._close_for_image_action(load_callback)
            )
        original_button = QPushButton("Original")
        original_button.setToolTip("Return to the uncropped original image")
        original_button.setEnabled(not self._original_image.isNull())
        original_button.clicked.connect(self._show_original)
        self._original_button = original_button
        millimeters = MeasurementSpinBox()
        millimeters.setRange(0.0, 1_000_000.0)
        millimeters.setDecimals(3)
        millimeters.setSuffix(" mm")
        millimeters.setToolTip("Real length of scale line")
        millimeters.valueChanged.connect(lambda _value: self._update_confirm_state())
        self._millimeters = millimeters
        rotate_left = QPushButton("Rotate left")
        rotate_left.clicked.connect(lambda: self._rotate(-90))
        rotate_right = QPushButton("Rotate right")
        rotate_right.clicked.connect(lambda: self._rotate(90))
        angle_spin = QDoubleSpinBox()
        angle_spin.setRange(-180.0, 180.0)
        angle_spin.setSingleStep(1.0)
        angle_spin.setSuffix("°")
        angle_spin.setToolTip("Free rotation angle")
        angle_spin.valueChanged.connect(self._set_angle)
        self._angle_spin = angle_spin
        zoom_out = QPushButton("−")
        zoom_out.setToolTip("Zoom out")
        zoom_out.clicked.connect(lambda: self._zoom_by(1 / 1.2))
        zoom_in = QPushButton("+")
        zoom_in.setToolTip("Zoom in")
        zoom_in.clicked.connect(lambda: self._zoom_by(1.2))
        fit_button = QPushButton("FIT")
        fit_button.setToolTip("Fit image in editor")
        fit_button.clicked.connect(self._fit_view)
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._mode_hint = QLabel()
        self._mode_hint.setStyleSheet("color: #66c2ff; font-weight: 600;")
        orientation_guide = QLabel()
        orientation_guide.setAlignment(Qt.AlignmentFlag.AlignCenter)
        orientation_guide.setToolTip(
            "Top-to-Bottom convention: turn the board 180° around its right edge"
        )
        guide = QPixmap(
            str(Path(__file__).parent.parent / "assets" / "board-right-edge-flip.png")
        )
        if side == "bottom" and not guide.isNull():
            orientation_guide.setPixmap(
                guide.scaled(
                    720,
                    260,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            orientation_guide.hide()
        self._orientation_guide = orientation_guide
        top_side_button = QPushButton("Top")
        top_side_button.setCheckable(True)
        top_side_button.setToolTip("Edit the Top board photo")
        top_side_button.clicked.connect(lambda: self._select_board_side("top"))
        bottom_side_button = QPushButton("Bottom")
        bottom_side_button.setCheckable(True)
        bottom_side_button.setToolTip("Edit the Bottom board photo")
        bottom_side_button.clicked.connect(lambda: self._select_board_side("bottom"))
        both_sides_button = QPushButton("Top + Bottom")
        both_sides_button.setCheckable(True)
        both_sides_button.setToolTip("Show both board photos at the same time")
        both_sides_button.clicked.connect(self._show_both_board_sides)
        self._top_side_button = top_side_button
        self._bottom_side_button = bottom_side_button
        self._both_sides_button = both_sides_button
        side_controls = QHBoxLayout()
        side_controls.addWidget(QLabel("Board photo"))
        side_controls.addWidget(top_side_button)
        side_controls.addWidget(bottom_side_button)
        side_controls.addWidget(both_sides_button)
        side_controls.addStretch()
        current_side_label = QLabel()
        current_side_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        current_side_label.setStyleSheet("font-weight: 600;")
        comparison_side_label = QLabel()
        comparison_side_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        comparison_side_label.setStyleSheet("font-weight: 600;")
        self._current_side_label = current_side_label
        self._comparison_side_label = comparison_side_label
        current_panel = QWidget()
        current_layout = QVBoxLayout(current_panel)
        current_layout.setContentsMargins(0, 0, 0, 0)
        current_layout.addWidget(current_side_label)
        current_layout.addWidget(self._scroll)
        comparison_panel = QWidget()
        comparison_layout = QVBoxLayout(comparison_panel)
        comparison_layout.setContentsMargins(0, 0, 0, 0)
        comparison_layout.addWidget(comparison_side_label)
        comparison_layout.addWidget(self._comparison_scroll)
        comparison_panel.hide()
        self._comparison_panel = comparison_panel
        picture_layout = QHBoxLayout()
        picture_layout.addWidget(current_panel, 1)
        picture_layout.addWidget(comparison_panel, 1)
        if side in {"top", "bottom"}:
            current_side_label.setText(side.title())
            comparison_side_label.setText("Bottom" if side == "top" else "Top")
            top_side_button.setChecked(side == "top")
            bottom_side_button.setChecked(side == "bottom")
        else:
            top_side_button.hide()
            bottom_side_button.hide()
            both_sides_button.hide()
        controls = QHBoxLayout()
        controls.addWidget(load_image_button)
        controls.addWidget(original_button)
        controls.addWidget(calibration_button)
        controls.addWidget(align_button)
        controls.addWidget(footprint_button)
        controls.addWidget(crop_button)
        controls.addWidget(QLabel("Real length"))
        controls.addWidget(millimeters)
        controls.addWidget(rotate_left)
        controls.addWidget(rotate_right)
        controls.addWidget(QLabel("Angle"))
        controls.addWidget(angle_spin)
        controls.addWidget(zoom_out)
        controls.addWidget(zoom_in)
        controls.addWidget(fit_button)
        controls.addStretch()
        controls.addWidget(self._buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Draw a scale line, or choose a KiCad footprint and resize it "
                "over the photo (drag the yellow corner handle to resize from "
                "the top-left, or the square to move it), then select the crop "
                "rectangle."
            )
        )
        layout.addWidget(orientation_guide)
        layout.addLayout(side_controls)
        layout.addWidget(self._mode_hint)
        layout.addLayout(picture_layout, 1)
        layout.addLayout(controls)
        self.showMaximized()
        self._render()
        QTimer.singleShot(0, self._render)
        self._set_edit_mode("calibration")

    def _set_side_button_state(self, mode: str) -> None:
        """Keep the board-photo view buttons mutually exclusive."""
        buttons = {
            "top": self._top_side_button,
            "bottom": self._bottom_side_button,
            "both": self._both_sides_button,
        }
        for name, button in buttons.items():
            button.setChecked(name == mode)

    def _select_board_side(self, side: str) -> None:
        """Save the current calibration before opening another board face."""
        if side == self._side:
            self._showing_both_sides = False
            self._comparison_panel.hide()
            self._set_side_button_state(side)
            self._render()
            return
        if self._side not in {"top", "bottom"}:
            return
        if not self._buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled():
            self._mode_hint.setText(
                "Complete the scale and crop before switching board side."
            )
            self._mode_hint.setStyleSheet("color: #ff8a80; font-weight: 600;")
            self._set_side_button_state(self._side)
            return
        self._requested_side = side
        self.accept()

    def _show_both_board_sides(self) -> None:
        """Show the active photo and the opposite face side by side."""
        if self._side not in {"top", "bottom"}:
            return
        self._showing_both_sides = True
        self._comparison_panel.show()
        self._set_side_button_state("both")
        self._render()
        QTimer.singleShot(0, self._render)

    def requested_side(self) -> str | None:
        """Return the face requested through the Top or Bottom button."""
        return self._requested_side

    def _render_comparison(self) -> None:
        """Fit the opposite board image in its read-only comparison panel."""
        if not self._showing_both_sides:
            return
        if self._comparison_image.isNull():
            other_side = "Bottom" if self._side == "top" else "Top"
            self._comparison_canvas.setPixmap(QPixmap())
            self._comparison_canvas.setText(f"No {other_side} picture")
            self._comparison_canvas.adjustSize()
            return
        self._comparison_canvas.setText("")
        available = self._comparison_scroll.maximumViewportSize()
        if available.width() < 2 or available.height() < 2:
            available = self._comparison_image.size()
        displayed = self._comparison_image.scaled(
            available,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._comparison_canvas.setPixmap(displayed)
        self._comparison_canvas.resize(displayed.size())

    def _close_for_image_action(self, callback: Callable[[], None]) -> None:
        """Close editor before changing its project-side image."""
        self.reject()
        QTimer.singleShot(0, callback)

    def _show_original(self) -> None:
        """Restore the uncropped source image so its crop can be changed."""
        if self._original_image.isNull():
            return
        self._base_image = QPixmap(self._original_image)
        self._source = QPixmap(self._original_image)
        self._angle = 0.0
        self._angle_spin.blockSignals(True)
        self._angle_spin.setValue(0.0)
        self._angle_spin.blockSignals(False)
        self._calibration_line = None
        self._calibration_start = None
        self._calibration_end = None
        self._calibration_footprint = None
        self._footprint_center = None
        self._footprint_pixels_per_mm = 0.0
        self._result_calibration_line = None
        self._millimeters.setValue(0.0)
        self._started_from_original = True
        self._crop_selection_modified = True
        self._selection = None
        self._selection_source_ratio = None
        self._selection_base_points = None
        self._rubber_band.hide()
        self._set_edit_mode("calibration")
        self._render(preserve_selection=False)
        self._set_selection(
            QPoint(0, 0),
            QPoint(max(0, self._canvas.width() - 1), max(0, self._canvas.height() - 1)),
        )
        self.show()
        self.raise_()
        self.activateWindow()

    def started_from_original(self) -> bool:
        """Return whether the current edit was rebuilt from the raw source."""
        return self._started_from_original

    def prepare_existing_image(
        self,
        pixels_per_mm: float,
        calibration_line: tuple[float, float, float, float] | None = None,
        calibration_length_mm: float | None = None,
    ) -> None:
        """Start editing with the existing crop and scale still valid."""
        if pixels_per_mm <= 0:
            return
        self._editing_existing_image = True
        self._crop_selection_modified = False
        width = max(2, self._canvas.width())
        height = max(2, self._canvas.height())
        self._set_selection(QPoint(0, 0), QPoint(width - 1, height - 1))
        if calibration_line is None:
            start = QPoint(0, max(0, height // 2))
            end = QPoint(width - 1, max(0, height // 2))
            self._set_calibration_line(start, end)
        else:
            # The calibration line may intentionally sit outside the crop. Keep
            # its source coordinates instead of clamping it to the working image.
            self._calibration_line = (
                QPointF(
                    calibration_line[0] * self._source.width(),
                    calibration_line[1] * self._source.height(),
                ),
                QPointF(
                    calibration_line[2] * self._source.width(),
                    calibration_line[3] * self._source.height(),
                ),
            )
        if self._calibration_line is not None:
            if calibration_length_mm is not None:
                self._millimeters.setValue(calibration_length_mm)
            else:
                self._millimeters.setValue(
                    QLineF(*self._calibration_line).length() / pixels_per_mm
                )
        self._update_confirm_state()

    def eventFilter(
        self, watched, event
    ) -> bool:  # noqa: N802  # pylint: disable=too-many-return-statements
        """Track rectangle selection on the image canvas."""
        if watched is not self._canvas:
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.Wheel:
            self._zoom_by(1.1 if event.angleDelta().y() > 0 else 1 / 1.1)
            return True
        if (
            event.type() == QEvent.Type.NativeGesture
            and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture
        ):
            gesture_delta = max(-0.1, min(0.1, event.value()))
            self._zoom_by(max(0.9, 1.0 + gesture_delta))
            return True
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
            and self._edit_mode == "crop"
            and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self._pan_position = event.globalPosition().toPoint()
            self._canvas.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return True
        if event.type() == QEvent.Type.MouseMove and self._pan_position is not None:
            current = event.globalPosition().toPoint()
            delta = current - self._pan_position
            self._pan_position = current
            horizontal = self._scroll.horizontalScrollBar()
            vertical = self._scroll.verticalScrollBar()
            horizontal.setValue(horizontal.value() - delta.x())
            vertical.setValue(vertical.value() - delta.y())
            return True
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
            and self._pan_position is not None
        ):
            self._pan_position = None
            self._canvas.unsetCursor()
            return True
        if self._edit_mode == "footprint":
            if event.type() == QEvent.Type.MouseButtonPress:
                self._footprint_drag_mode = None
                if event.button() == Qt.MouseButton.RightButton:
                    self._footprint_rotation = (self._footprint_rotation + 45.0) % 360.0
                    self._render()
                    return True
                if event.button() == Qt.MouseButton.LeftButton:
                    point = self._footprint_source_point(event.position())
                    center = self._footprint_center
                    footprint = self._calibration_footprint
                    if center is None or footprint is None:
                        return True
                    radius = footprint.radius() * self._footprint_pixels_per_mm
                    handle = center + QPointF(radius, radius)
                    hit_radius = min(
                        max(16.0 / self._display_scale, 4.0),
                        max(radius * 0.75, 4.0),
                    )
                    if QLineF(point, handle).length() <= hit_radius:
                        self._footprint_drag_mode = "scale"
                        self._footprint_scale_anchor = center - QPointF(radius, radius)
                    elif QRectF(
                        center.x() - radius,
                        center.y() - radius,
                        radius * 2,
                        radius * 2,
                    ).contains(point):
                        self._footprint_drag_mode = "move"
                        self._footprint_drag_offset = point - center
                        self._footprint_scale_anchor = None
                    else:
                        self._footprint_drag_mode = None
                        self._footprint_scale_anchor = None
                    return True
            if event.type() == QEvent.Type.MouseMove and self._footprint_drag_mode:
                point = self._footprint_source_point(event.position())
                if self._footprint_drag_mode == "scale":
                    anchor = self._footprint_scale_anchor
                    footprint = self._calibration_footprint
                    if anchor is None or footprint is None:
                        return True
                    footprint_radius = max(footprint.radius(), 0.1)
                    side = max(point.x() - anchor.x(), point.y() - anchor.y(), 0.2)
                    radius = side / 2.0
                    self._footprint_pixels_per_mm = max(0.01, radius / footprint_radius)
                    self._footprint_center = anchor + QPointF(radius, radius)
                else:
                    self._footprint_center = point - self._footprint_drag_offset
                self._render()
                self._update_confirm_state()
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._footprint_drag_mode = None
                self._footprint_scale_anchor = None
                return True
            return super().eventFilter(watched, event)
        if self._edit_mode == "align":
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._calibration_start = event.position().toPoint()
                self._calibration_end = self._calibration_start
                self._render()
                return True
            if event.type() == QEvent.Type.MouseMove and self._calibration_start:
                self._calibration_end = event.position().toPoint()
                self._render()
                return True
            if (
                event.type() == QEvent.Type.MouseButtonRelease
                and self._calibration_start
            ):
                self._calibration_end = event.position().toPoint()
                start = self._calibration_start
                end = self._calibration_end
                self._calibration_start = None
                self._calibration_end = None
                if QLineF(start, end).length() >= 2:
                    angle = math.degrees(
                        math.atan2(end.y() - start.y(), end.x() - start.x())
                    )
                    self._set_angle(self._angle - angle)
                self._set_edit_mode("calibration")
                return True
            return super().eventFilter(watched, event)
        if self._edit_mode == "calibration":
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._calibration_start = event.position().toPoint()
                self._calibration_end = self._calibration_start
                self._render()
                return True
            if event.type() == QEvent.Type.MouseMove and self._calibration_start:
                self._calibration_end = event.position().toPoint()
                self._render()
                return True
            if (
                event.type() == QEvent.Type.MouseButtonRelease
                and self._calibration_start
            ):
                self._calibration_end = event.position().toPoint()
                self._set_calibration_line(
                    self._calibration_start, self._calibration_end
                )
                self._calibration_start = None
                self._calibration_end = None
                return True
            return super().eventFilter(watched, event)
        if (
            self._edit_mode == "crop"
            and event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
            and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            return True
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._crop_selection_modified = True
            position = event.position().toPoint()
            self._resize_edges = self._hit_edges(position)
            self._selection_start = position
            if not self._resize_edges:
                self._selection = None
                self._selection_source_ratio = None
                self._selection_base_points = None
                self._rubber_band.hide()
                self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
                    False
                )
            return True
        if event.type() == QEvent.Type.MouseMove and self._selection_start is not None:
            position = event.position().toPoint()
            if self._resize_edges:
                self._resize_selection(position)
            else:
                self._set_selection(self._selection_start, position)
            return True
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and self._selection_start is not None
        ):
            position = event.position().toPoint()
            if self._resize_edges:
                self._resize_selection(position)
            else:
                self._set_selection(self._selection_start, position)
            self._selection_start = None
            self._resize_edges = set()
            return True
        if event.type() == QEvent.Type.MouseMove:
            self._set_resize_cursor(event.position().toPoint())
            return False
        return super().eventFilter(watched, event)

    def _footprint_source_point(self, point: QPointF) -> QPointF:
        """Convert a canvas point to source-image coordinates."""
        return QPointF(
            point.x() / self._display_scale,
            point.y() / self._display_scale,
        )

    def accept(self) -> None:
        """Crop selected area and close editor."""
        if (
            self._selection is None
            or self._selection.width() < 2
            or self._selection.height() < 2
            or not self._has_calibration()
        ):
            QMessageBox.warning(
                self,
                "Incomplete import",
                "Define scale line, real length, and crop rectangle first.",
            )
            return
        selection = self._selection
        source_rect = (
            QRect(0, 0, self._source.width(), self._source.height())
            if self._editing_existing_image and not self._crop_selection_modified
            else self._source_rect(selection)
        )
        self._result_transformation = (
            self._angle,
            (
                source_rect.x() / self._source.width(),
                source_rect.y() / self._source.height(),
                source_rect.width() / self._source.width(),
                source_rect.height() / self._source.height(),
            ),
        )
        calibration_line = self._active_calibration_line()
        if calibration_line is not None:
            start, end = calibration_line
            self._result_calibration_line = (
                (start.x() - source_rect.x()) / source_rect.width(),
                (start.y() - source_rect.y()) / source_rect.height(),
                (end.x() - source_rect.x()) / source_rect.width(),
                (end.y() - source_rect.y()) / source_rect.height(),
            )
        self._source = self._source.copy(source_rect)
        super().accept()

    def result_pixmap(self) -> QPixmap:
        """Return edited image after accepted dialog."""
        return self._source

    def pixels_per_mm(self) -> float:
        """Return source pixels per real millimeter from calibration line."""
        if self._calibration_method == "footprint":
            return self._footprint_pixels_per_mm
        if self._calibration_line is None or self._millimeters.value() <= 0:
            return 0.0
        return QLineF(*self._calibration_line).length() / self._millimeters.value()

    def calibration_line(self) -> tuple[float, float, float, float] | None:
        """Return accepted scale-line coordinates normalized to output image."""
        return self._result_calibration_line

    def calibration_length_mm(self) -> float:
        """Return the exact real-world length entered for the scale line."""
        if self._calibration_method == "footprint":
            return self._footprint_reference_length_mm()
        return self._millimeters.value()

    def transformation(self) -> tuple[float, tuple[float, float, float, float]]:
        """Return accepted rotation and crop relative to the input image."""
        if self._result_transformation is None:
            raise ProjectFormatError("image transformation is not available")
        return self._result_transformation

    def _set_edit_mode(self, mode: str) -> None:
        """Switch between scale-line and crop-rectangle drawing."""
        self._edit_mode = mode
        if mode == "calibration":
            self._calibration_method = "line"
            self._millimeters.setEnabled(True)
        elif mode == "footprint":
            self._calibration_method = "footprint"
            self._millimeters.setEnabled(False)
        elif mode == "align":
            self._millimeters.setEnabled(False)
        self._calibration_button.setChecked(mode == "calibration")
        self._align_button.setChecked(mode == "align")
        self._footprint_button.setChecked(mode == "footprint")
        self._crop_button.setChecked(mode == "crop")
        hints = {
            "calibration": "Scale line: draw from point to point.",
            "align": "Draw a line to rotate it horizontal.",
            "footprint": "Scale footprint: drag to position or resize.",
            "crop": "Use Shift to draw border",
        }
        self._mode_hint.setText(hints[mode])

    def _choose_footprint_calibration(self) -> None:
        """Select and place a KiCad footprint for physical calibration."""
        if self._footprint_selector is None:
            return
        footprint = self._footprint_selector(self)
        if footprint is None:
            if self._calibration_method == "footprint":
                self._set_edit_mode("calibration")
                self._render()
            self.show()
            self.raise_()
            self.activateWindow()
            return
        self._calibration_footprint = footprint
        self._footprint_center = QPointF(
            self._source.width() / 2, self._source.height() / 2
        )
        self._footprint_pixels_per_mm = max(
            0.01,
            self._source.width() / (4.0 * max(footprint.radius(), 0.1)),
        )
        self._footprint_rotation = 0.0
        self._set_edit_mode("footprint")
        self._update_confirm_state()
        self._render()
        self.show()
        self.raise_()
        self.activateWindow()

    def _footprint_reference_length_mm(self) -> float:
        """Return the known reference diameter used by footprint calibration."""
        if self._calibration_footprint is None:
            return 0.0
        return max(0.1, self._calibration_footprint.radius() * 2.0)

    def _active_calibration_line(self) -> tuple[QPointF, QPointF] | None:
        """Return the selected calibration line or a synthetic footprint line."""
        if self._calibration_method == "line":
            return self._calibration_line
        if self._footprint_center is None or self._calibration_footprint is None:
            return None
        half_length = self._footprint_reference_length_mm() / 2
        half_pixels = half_length * self._footprint_pixels_per_mm
        return (
            QPointF(
                self._footprint_center.x() - half_pixels, self._footprint_center.y()
            ),
            QPointF(
                self._footprint_center.x() + half_pixels, self._footprint_center.y()
            ),
        )

    def _has_calibration(self) -> bool:
        """Return whether the active calibration method has valid dimensions."""
        if self._calibration_method == "footprint":
            return (
                self._calibration_footprint is not None
                and self._footprint_center is not None
                and self._footprint_pixels_per_mm > 0
            )
        return self._calibration_line is not None and self._millimeters.value() > 0

    def _set_calibration_line(self, start: QPoint, end: QPoint) -> None:
        """Store calibration endpoints in source-image coordinates."""
        start = self._clamp_point(start, self._canvas.rect())
        end = self._clamp_point(end, self._canvas.rect())
        if QLineF(start, end).length() < 2:
            return
        self._calibration_line = (
            QPointF(start.x() / self._display_scale, start.y() / self._display_scale),
            QPointF(end.x() / self._display_scale, end.y() / self._display_scale),
        )
        self._update_confirm_state()

    def _update_confirm_state(self) -> None:
        """Enable import only after calibration and crop are complete."""
        enabled = (
            self._selection is not None
            and self._selection.width() >= 2
            and self._selection.height() >= 2
            and self._has_calibration()
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(enabled)

    def _rotate(self, angle: int) -> None:
        """Rotate the source image and its crop selection."""
        self._set_angle(self._angle + angle)

    def _set_angle(self, angle: float) -> None:
        """Apply free rotation relative to original imported image."""
        self._zoom_revision += 1
        old_angle = self._angle
        old_matrix = self._rotation_matrix(old_angle)
        old_inverse, invertible = old_matrix.inverted()
        new_matrix = self._rotation_matrix(angle)

        def remap(point: QPointF) -> QPointF:
            return new_matrix.map(old_inverse.map(point))

        selection_base_points = self._selection_base_points
        if selection_base_points is None and self._selection is not None and invertible:
            source = QRectF(self._source_rect(self._selection))
            selection_base_points = tuple(
                old_inverse.map(point) for point in self._rectangle_points(source)
            )
        self._angle = angle
        self._angle_spin.blockSignals(True)
        self._angle_spin.setValue(angle)
        self._angle_spin.blockSignals(False)
        self._source = self._base_image.transformed(
            QTransform().rotate(angle), Qt.TransformationMode.SmoothTransformation
        )
        if self._calibration_line is not None and invertible:
            self._calibration_line = tuple(
                remap(point) for point in self._calibration_line
            )
        if self._footprint_center is not None and invertible:
            self._footprint_center = remap(self._footprint_center)
            self._footprint_rotation = (
                self._footprint_rotation + angle - old_angle
            ) % 360.0
        self._update_confirm_state()
        self._selection = None
        self._selection_source_ratio = None
        self._rubber_band.hide()
        self._render(preserve_selection=False)
        if selection_base_points is not None:
            mapped = tuple(new_matrix.map(point) for point in selection_base_points)
            left = min(point.x() for point in mapped)
            top = min(point.y() for point in mapped)
            right = max(point.x() for point in mapped)
            bottom = max(point.y() for point in mapped)
            x = round(left * self._display_scale)
            y = round(top * self._display_scale)
            selection = QRect(
                x,
                y,
                round(right * self._display_scale) - x,
                round(bottom * self._display_scale) - y,
            ).intersected(self._canvas.rect())
            if selection.width() >= 2 and selection.height() >= 2:
                self._selection_base_points = selection_base_points
                self._update_selection(selection, store_base=False)

    def _zoom_by(self, factor: float, anchor: QPoint | None = None) -> None:
        """Zoom around anchor point, preserving content under cursor."""
        centered = anchor is None
        self._zoom_revision += 1
        revision = self._zoom_revision
        anchor = anchor or QPoint(
            self._scroll.viewport().width() // 2,
            self._scroll.viewport().height() // 2,
        )
        old_effective = self._display_scale
        old_canvas_point = self._canvas.mapFrom(self._scroll.viewport(), anchor)
        source_point = QPointF(
            old_canvas_point.x() / old_effective,
            old_canvas_point.y() / old_effective,
        )
        self._zoom = max(0.1, min(self._zoom * factor, 20.0))
        self._render()
        zoom_anchor = None if centered else anchor
        self._restore_zoom_anchor(source_point, zoom_anchor)
        QTimer.singleShot(
            0,
            lambda: self._finish_zoom_anchor(revision, source_point, zoom_anchor),
        )

    def _finish_zoom_anchor(
        self, revision: int, source_point: QPointF, anchor: QPoint | None
    ) -> None:
        """Correct zoom anchoring after Qt has laid out new scrollbars."""
        if revision == self._zoom_revision:
            self._restore_zoom_anchor(source_point, anchor)

    def _restore_zoom_anchor(
        self, source_point: QPointF, anchor: QPoint | None
    ) -> None:
        """Keep one source-image point fixed in the editor viewport."""
        if anchor is None:
            anchor = QPoint(
                self._scroll.viewport().width() // 2,
                self._scroll.viewport().height() // 2,
            )
        else:
            anchor = self._clamp_point(anchor, self._scroll.viewport().rect())
        new_canvas_point = QPoint(
            round(source_point.x() * self._display_scale),
            round(source_point.y() * self._display_scale),
        )
        current_canvas_point = self._canvas.mapFrom(self._scroll.viewport(), anchor)
        horizontal = self._scroll.horizontalScrollBar()
        vertical = self._scroll.verticalScrollBar()
        horizontal.setValue(
            horizontal.value() + new_canvas_point.x() - current_canvas_point.x()
        )
        vertical.setValue(
            vertical.value() + new_canvas_point.y() - current_canvas_point.y()
        )

    def _fit_view(self) -> None:
        """Reset editor preview zoom to fit."""
        self._zoom_revision += 1
        selection_ratio = self._selection_ratio()
        self._zoom = 1.0
        self._render(preserve_selection=False)
        self._restore_selection(selection_ratio)

    def _render(self, preserve_selection: bool = True) -> None:
        """Fit source image to editor viewport."""
        selection_ratio = self._selection_ratio() if preserve_selection else None
        # maximumViewportSize() excludes the feedback caused by scrollbars
        # appearing during a zoom operation, so FIT remains a stable baseline.
        viewport = self._scroll.maximumViewportSize()
        width_ratio = viewport.width() / self._source.width()
        height_ratio = viewport.height() / self._source.height()
        self._display_scale = min(1.0, width_ratio, height_ratio) * self._zoom
        displayed = self._source.scaled(
            self._source.size() * self._display_scale,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter = QPainter(displayed)
        if self._calibration_line is not None:
            start, end = self._calibration_line
            painter.setPen(QPen(Qt.GlobalColor.red, 3))
            painter.drawLine(
                QPoint(
                    round(start.x() * self._display_scale),
                    round(start.y() * self._display_scale),
                ),
                QPoint(
                    round(end.x() * self._display_scale),
                    round(end.y() * self._display_scale),
                ),
            )
        if self._calibration_start is not None and self._calibration_end is not None:
            painter.setPen(QPen(Qt.GlobalColor.yellow, 3))
            painter.drawLine(self._calibration_start, self._calibration_end)
        if self._calibration_method == "footprint":
            center = self._footprint_center
            footprint = self._calibration_footprint
            if center is not None and footprint is not None:
                painter.save()
                painter.translate(
                    center.x() * self._display_scale,
                    center.y() * self._display_scale,
                )
                _paint_footprint(
                    painter,
                    footprint,
                    self._footprint_pixels_per_mm * self._display_scale,
                    "top",
                    self._footprint_rotation,
                    preview=True,
                )
                radius = (
                    footprint.radius()
                    * self._footprint_pixels_per_mm
                    * self._display_scale
                )
                painter.setPen(QPen(QColor(255, 255, 0), 2, Qt.PenStyle.DashLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(QRectF(-radius, -radius, radius * 2, radius * 2))
                painter.setBrush(QColor(255, 255, 0))
                painter.drawEllipse(QPointF(radius, radius), 6, 6)
                painter.restore()
        painter.end()
        self._canvas.setPixmap(displayed)
        self._canvas.resize(displayed.size())
        self._rubber_band.setGeometry(self._canvas.rect())
        self._rubber_band.set_selection(self._selection)
        if self._selection is not None:
            self._rubber_band.show()
        self._restore_selection(selection_ratio)
        self._render_comparison()

    def _set_selection(self, start: QPoint, end: QPoint) -> None:
        """Update selection rectangle constrained to displayed image."""
        bounds = self._canvas.rect()
        start = self._clamp_point(start, bounds)
        end = self._clamp_point(end, bounds)
        self._update_selection(QRect(start, end).normalized())

    def _selection_ratio(self) -> tuple[float, float, float, float] | None:
        """Return selection bounds as ratios of the source image."""
        if self._selection_source_ratio is not None:
            return self._selection_source_ratio
        if self._selection is None:
            return None
        source = self._source_rect(self._selection)
        width, height = self._source.width(), self._source.height()
        return (
            source.x() / width,
            source.y() / height,
            source.width() / width,
            source.height() / height,
        )

    def _restore_selection(
        self, selection_ratio: tuple[float, float, float, float] | None
    ) -> None:
        """Reproject selection bounds after the preview scale changes."""
        if selection_ratio is None:
            return
        width, height = self._source.width(), self._source.height()
        selection = QRect(
            round(selection_ratio[0] * width * self._display_scale),
            round(selection_ratio[1] * height * self._display_scale),
            round(selection_ratio[2] * width * self._display_scale),
            round(selection_ratio[3] * height * self._display_scale),
        ).intersected(self._canvas.rect())
        if selection.width() >= 2 and selection.height() >= 2:
            self._update_selection(selection, store_source=False)

    def _update_selection(
        self,
        selection: QRect,
        store_source: bool = True,
        store_base: bool = True,
    ) -> None:
        """Apply selection geometry and update validation state."""
        self._selection = selection
        if store_source:
            source = self._source_rect(selection)
            width, height = self._source.width(), self._source.height()
            self._selection_source_ratio = (
                source.x() / width,
                source.y() / height,
                source.width() / width,
                source.height() / height,
            )
            if store_base:
                inverse, invertible = self._rotation_matrix(self._angle).inverted()
                self._selection_base_points = (
                    tuple(
                        inverse.map(point)
                        for point in self._rectangle_points(QRectF(source))
                    )
                    if invertible
                    else None
                )
        self._rubber_band.setGeometry(self._canvas.rect())
        self._rubber_band.set_selection(self._selection)
        self._rubber_band.show()
        self._update_confirm_state()

    def _rotation_matrix(self, angle: float) -> QTransform:
        """Return Qt's translated rotation matrix for the uncropped image."""
        return QPixmap.trueMatrix(
            QTransform().rotate(angle),
            self._base_image.width(),
            self._base_image.height(),
        )

    @staticmethod
    def _rectangle_points(rectangle: QRectF) -> tuple[QPointF, ...]:
        """Return rectangle boundary corners without integer-edge ambiguity."""
        return (
            QPointF(rectangle.left(), rectangle.top()),
            QPointF(rectangle.left() + rectangle.width(), rectangle.top()),
            QPointF(
                rectangle.left() + rectangle.width(),
                rectangle.top() + rectangle.height(),
            ),
            QPointF(rectangle.left(), rectangle.top() + rectangle.height()),
        )

    def _hit_edges(self, point: QPoint) -> set[str]:
        """Return selection edges near point for individual edge dragging."""
        if self._selection is None:
            return set()
        margin = 8
        edges: set[str] = set()
        if abs(point.x() - self._selection.left()) <= margin:
            edges.add("left")
        if abs(point.x() - self._selection.right()) <= margin:
            edges.add("right")
        if abs(point.y() - self._selection.top()) <= margin:
            edges.add("top")
        if abs(point.y() - self._selection.bottom()) <= margin:
            edges.add("bottom")
        expanded = self._selection.adjusted(-margin, -margin, margin, margin)
        return edges if expanded.contains(point) else set()

    def _resize_selection(self, point: QPoint) -> None:
        """Move selected rectangle edges to point."""
        if self._selection is None:
            return
        point = self._clamp_point(point, self._canvas.rect())
        selection = QRect(self._selection)
        if "left" in self._resize_edges:
            selection.setLeft(min(point.x(), selection.right() - 2))
        if "right" in self._resize_edges:
            selection.setRight(max(point.x(), selection.left() + 2))
        if "top" in self._resize_edges:
            selection.setTop(min(point.y(), selection.bottom() - 2))
        if "bottom" in self._resize_edges:
            selection.setBottom(max(point.y(), selection.top() + 2))
        self._update_selection(selection)

    def _set_resize_cursor(self, point: QPoint) -> None:
        """Show cursor matching resize edge under pointer."""
        edges = self._hit_edges(point)
        if edges in ({"left", "right"},):
            cursor = Qt.CursorShape.SizeHorCursor
        elif edges in ({"top", "bottom"},):
            cursor = Qt.CursorShape.SizeVerCursor
        elif edges in ({"left", "bottom"}, {"right", "top"}):
            cursor = Qt.CursorShape.SizeBDiagCursor
        elif edges in ({"left", "top"}, {"right", "bottom"}):
            cursor = Qt.CursorShape.SizeFDiagCursor
        else:
            self._canvas.unsetCursor()
            return
        self._canvas.setCursor(QCursor(cursor))

    @staticmethod
    def _clamp_point(point: QPoint, bounds: QRect) -> QPoint:
        """Constrain point to image canvas bounds."""
        return QPoint(
            max(bounds.left(), min(point.x(), bounds.right())),
            max(bounds.top(), min(point.y(), bounds.bottom())),
        )

    def _source_rect(self, selection: QRect) -> QRect:
        """Convert displayed selection to source-pixel coordinates."""
        return QRect(
            int(selection.x() / self._display_scale),
            int(selection.y() / self._display_scale),
            int(selection.width() / self._display_scale),
            int(selection.height() / self._display_scale),
        )
