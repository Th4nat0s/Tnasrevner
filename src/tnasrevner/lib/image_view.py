"""Board-image tab widget, overlays, interaction, pan, and zoom."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,invalid-name,unused-import,too-many-lines
# pylint: disable=duplicate-code
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

from .footprints import FootprintPreview, _paint_footprint
from .app_support import LOGGER


class ImageView(
    QScrollArea
):  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Scrollable image view with mouse-wheel zoom."""

    zoom_changed = Signal(float)
    MIN_ZOOM = 1.0
    MAX_ZOOM = 10.0
    _CONNECTION_LINE_WIDTH = 3.0

    view_changed = Signal()
    pad_selected = Signal(float, float, float, float)
    pad_clicked = Signal(float, float)
    pad_context_requested = Signal(float, float)
    pad_menu_requested = Signal(float, float)
    trace_menu_requested = Signal(float, float)
    pad_connection_requested = Signal(float, float)
    device_placed = Signal(float, float)
    device_rotated = Signal()
    delete_requested = Signal(float, float)
    delete_mode_changed = Signal(bool)
    ruler_measured = Signal(float)
    pad_hovered = Signal(object)

    def __init__(self, empty_text: str) -> None:
        super().__init__()
        self._empty_text = empty_text
        self._pixmap = QPixmap()
        self._scale = 1.0
        self._zoom_revision = 0
        self._zoom_render_pending = False
        self._pending_view_state: tuple[float, float, float] | None = None
        self._drag_position: QPoint | None = None
        self._temporary_pan = False
        self._pad_placement = False
        self._pad_start: QPoint | None = None
        self._click_position: QPoint | None = None
        self._device_placement = False
        self._delete_mode = False
        self._device_preview_point = (0.5, 0.5)
        self._ruler_enabled = False
        self._ruler_pixels_per_mm = 0.0
        self._ruler_start: tuple[float, float] | None = None
        self._ruler_end: tuple[float, float] | None = None
        self._hover_pad_id: str | None = None
        self._pad_labels: tuple[Pad, ...] = ()
        self._mirror_pad_labels = False
        self._footprint_overlays: tuple[tuple, ...] = ()
        self._connection_net: str | None = None
        self._connection_origin_id: str | None = None
        self._connection_trace_pairs: tuple[tuple[str, str], ...] | None = None
        self._connection_highlight_ids: frozenset[str] | None = None
        self._connection_preview_origin: tuple[float, float] | None = None
        self._connection_preview_cursor: tuple[float, float] | None = None
        self._label = QLabel(empty_text)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setMinimumSize(240, 180)
        self._label.setMouseTracking(True)
        self._label.installEventFilter(self)
        self._device_preview = FootprintPreview(self._label)
        self.viewport().installEventFilter(self)
        self.viewport().setMouseTracking(True)
        self._pad_band = QRubberBand(QRubberBand.Shape.Rectangle, self._label)
        self._pad_band.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self.setWidget(self._label)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.horizontalScrollBar().valueChanged.connect(self.view_changed)
        self.verticalScrollBar().valueChanged.connect(self.view_changed)

    def set_image(self, path: Path | None) -> None:
        """Display image at its native scale, or show an empty state."""
        self._zoom_revision += 1
        self._zoom_render_pending = False
        self._pixmap = QPixmap(str(path)) if path else QPixmap()
        self._scale = 1.0
        self._render()

    def set_image_data(self, content: bytes) -> None:
        """Display image bytes loaded from a `.revp` archive."""
        self._zoom_revision += 1
        self._zoom_render_pending = False
        self._pixmap = QPixmap()
        self._pixmap.loadFromData(content)
        self._scale = 1.0
        self._render()

    def set_pixmap(self, pixmap: QPixmap) -> None:
        """Display an already composed pixmap."""
        self._zoom_revision += 1
        self._zoom_render_pending = False
        self._pixmap = pixmap
        self._scale = 1.0
        self._render()

    def set_pad_labels(self, pads: tuple[Pad, ...], mirror_x: bool = False) -> None:
        """Render pad labels at the current zoom instead of baking them in."""
        self._pad_labels = pads
        self._mirror_pad_labels = mirror_x
        self._render()

    def set_footprint_overlays(self, footprints: tuple[tuple, ...]) -> None:
        """Render placed KiCad footprints at the current zoom resolution."""
        self._footprint_overlays = footprints
        self._render()

    def set_trace_selection(
        self,
        net: str | None,
        origin_id: str | None,
        trace_pairs: tuple[tuple[str, str], ...] | None = None,
        highlight_ids: frozenset[str] | None = None,
    ) -> None:
        """Store visible trace selection for hit testing."""
        self._connection_net = net
        self._connection_origin_id = origin_id
        self._connection_trace_pairs = trace_pairs
        self._connection_highlight_ids = highlight_ids
        self._render()

    def trace_at(self, x: float, y: float) -> tuple[str, str] | None:
        """Return trace endpoints near normalized image coordinates."""
        if self._label.width() <= 1 or self._label.height() <= 1:
            return None
        point = QPointF(x * (self._label.width() - 1), y * (self._label.height() - 1))
        return self._trace_at_point(point)

    def set_connection_mode(self, enabled: bool) -> None:
        """Use crosshair cursor while editing a pad connection."""
        if enabled:
            cursor = QCursor(Qt.CursorShape.CrossCursor)
            self.setCursor(cursor)
            self._label.setCursor(cursor)
        else:
            self.unsetCursor()
            self._label.unsetCursor()
            self.clear_connection_preview()

    def set_connection_preview_origin(self, x: float, y: float) -> None:
        """Start a temporary line from the first selected board pad.

        Args:
            x: Normalized image coordinate of the origin.
            y: Normalized image coordinate of the origin.
        """
        origin = (max(0.0, min(1.0, x)), max(0.0, min(1.0, y)))
        self._connection_preview_origin = origin
        self._connection_preview_cursor = origin
        self._render()

    def clear_connection_preview(self) -> None:
        """Remove the non-persistent board connection preview line."""
        if (
            self._connection_preview_origin is None
            and self._connection_preview_cursor is None
        ):
            return
        self._connection_preview_origin = None
        self._connection_preview_cursor = None
        self._render()

    def set_ruler(self, enabled: bool, pixels_per_mm: float = 0.0) -> None:
        """Enable the two-click ruler using the image calibration."""
        self._ruler_enabled = enabled and pixels_per_mm > 0
        self._ruler_pixels_per_mm = pixels_per_mm
        self._ruler_start = None
        self._ruler_end = None
        self._render()

    def wheelEvent(self, event) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Zoom image with a mouse wheel or trackpad scroll gesture."""
        if event.angleDelta().y() and not self._pixmap.isNull():
            self._zoom_by(
                1.2 if event.angleDelta().y() > 0 else 1 / 1.2,
                event.position().toPoint(),
            )
            event.accept()
            return
        super().wheelEvent(event)

    def event(self, event) -> bool:  # noqa: N802
        """Handle macOS trackpad pinch-to-zoom gestures."""
        if (
            event.type() == QEvent.Type.NativeGesture
            and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture
            and not self._pixmap.isNull()
        ):
            self._zoom_by(max(0.01, 1.0 + event.value()))
            return True
        return super().event(event)

    def eventFilter(  # noqa: N802  # pylint: disable=too-many-return-statements,too-many-branches,too-many-statements
        self, watched, event
    ) -> bool:
        """Pan image by dragging it with the primary mouse button."""
        label = getattr(self, "_label", None)
        if label is None:
            return False
        if watched not in (label, self.viewport()):
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            if self._ruler_enabled:
                self.set_ruler(False)
                return True
            if self._delete_mode:
                self.set_delete_mode(False)
                self.delete_mode_changed.emit(False)
                return True
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.Leave:
            if self._hover_pad_id is not None:
                self._hover_pad_id = None
                self.pad_hovered.emit(None)
            self.clear_connection_preview()
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.MouseButtonDblClick:
            point = self._label_point(watched, event.position().toPoint())
            if (
                self._delete_mode
                and event.button() == Qt.MouseButton.LeftButton
                and not self._pixmap.isNull()
                and self._label.rect().contains(point)
            ):
                self.delete_requested.emit(*self._normalized_point(point))
                return True
            if (
                event.button() == Qt.MouseButton.LeftButton
                and not self._device_placement
                and not self._pad_placement
                and self._pad_at_point(point) is not None
            ):
                self.pad_clicked.emit(*self._normalized_point(point))
                self._click_position = None
                self._drag_position = None
            return True
        if event.type() == QEvent.Type.MouseButtonPress:
            point = self._label_point(watched, event.position().toPoint())
            if (
                self._delete_mode
                and event.button() == Qt.MouseButton.LeftButton
                and not self._pixmap.isNull()
                and self._label.rect().contains(point)
            ):
                self.delete_requested.emit(*self._normalized_point(point))
                return True
            if (
                event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.MetaModifier
                and (self._pad_placement or self._device_placement)
            ):
                self._temporary_pan = True
                self._drag_position = event.globalPosition().toPoint()
                self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
                return True
            if self._ruler_enabled and event.button() == Qt.MouseButton.LeftButton:
                if not self._label.rect().contains(point):
                    return True
                normalized = self._normalized_point(point)
                if self._ruler_start is None or self._ruler_end is not None:
                    self._ruler_start = normalized
                    self._ruler_end = None
                else:
                    self._ruler_end = normalized
                    self.ruler_measured.emit(self._ruler_measurement_mm())
                self._render()
                return True
            if (
                self._device_placement
                and not self._pixmap.isNull()
                and self._label.rect().contains(point)
            ):
                if event.button() == Qt.MouseButton.RightButton:
                    self.device_rotated.emit()
                    return True
                if event.button() == Qt.MouseButton.LeftButton:
                    self.device_placed.emit(*self._normalized_point(point))
                    return True
            if event.button() == Qt.MouseButton.RightButton:
                if not self._pixmap.isNull() and self._label.rect().contains(point):
                    if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                        self.pad_context_requested.emit(*self._normalized_point(point))
                    elif self._trace_at_point(point) is not None:
                        self.trace_menu_requested.emit(*self._normalized_point(point))
                    else:
                        self.pad_menu_requested.emit(*self._normalized_point(point))
                    return True
                return super().eventFilter(watched, event)
            if event.button() != Qt.MouseButton.LeftButton:
                return super().eventFilter(watched, event)
            if (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                and not self._pixmap.isNull()
                and self._label.rect().contains(point)
                and self._pad_at_point(point) is not None
            ):
                self.pad_connection_requested.emit(*self._normalized_point(point))
                return True
            if self._pad_placement and not self._pixmap.isNull():
                if self._label.rect().contains(point):
                    self._pad_start = point
                    self._pad_band.setGeometry(QRect(point, point))
                    self._pad_band.show()
                    LOGGER.debug("Pad drag started point=(%s,%s)", point.x(), point.y())
                    return True
            if self._pad_at_point(point) is not None:
                self._click_position = point
                self._drag_position = None
            else:
                self._click_position = None
                self._drag_position = event.globalPosition().toPoint()
                self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return True
        if event.type() == QEvent.Type.MouseMove and self._temporary_pan:
            current = event.globalPosition().toPoint()
            if self._drag_position is not None:
                delta = current - self._drag_position
                self.horizontalScrollBar().setValue(
                    self.horizontalScrollBar().value() - delta.x()
                )
                self.verticalScrollBar().setValue(
                    self.verticalScrollBar().value() - delta.y()
                )
                self._drag_position = current
            return True
        if event.type() == QEvent.Type.MouseMove and not (
            self._temporary_pan
            or self._ruler_enabled
            or self._pad_placement
            or self._device_placement
            or self._drag_position is not None
        ):
            point = self._label_point(watched, event.position().toPoint())
            hovered = self._pad_at_point(point)
            hovered_id = hovered.pad_id if hovered is not None else None
            if hovered_id != self._hover_pad_id:
                self._hover_pad_id = hovered_id
                self.pad_hovered.emit(hovered)
        if event.type() == QEvent.Type.MouseMove and self._connection_preview_origin:
            point = self._label_point(watched, event.position().toPoint())
            if self._label.rect().contains(point):
                self._connection_preview_cursor = self._normalized_point(point)
                self._render()
        if event.type() == QEvent.Type.MouseMove and self._ruler_enabled:
            point = self._label_point(watched, event.position().toPoint())
            if self._ruler_start is not None and self._label.rect().contains(point):
                self._ruler_end = self._normalized_point(point)
                self._render()
            return True
        if event.type() == QEvent.Type.MouseMove and self._device_placement:
            point = self._label_point(watched, event.position().toPoint())
            if self._label.rect().contains(point):
                self._device_preview_point = self._normalized_point(point)
                self._position_device_preview()
                self._device_preview.show()
            else:
                self._device_preview.hide()
            return True
        if event.type() == QEvent.Type.MouseMove and self._pad_start is not None:
            point = self._clamp_point(
                self._label_point(watched, event.position().toPoint()),
                self._label.rect(),
            )
            self._pad_band.setGeometry(QRect(self._pad_start, point).normalized())
            return True
        if event.type() == QEvent.Type.MouseMove and self._drag_position is not None:
            current = event.globalPosition().toPoint()
            delta = current - self._drag_position
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )
            self._drag_position = current
            return True
        if event.type() == QEvent.Type.MouseButtonRelease:
            if self._temporary_pan:
                self._temporary_pan = False
                self._drag_position = None
                self.unsetCursor()
                return True
            point = self._clamp_point(
                self._label_point(watched, event.position().toPoint()),
                self._label.rect(),
            )
            if self._pad_start is not None:
                selection = QRect(self._pad_start, point).normalized()
                self._pad_start = None
                self._pad_band.hide()
                if selection.width() >= 2 and selection.height() >= 2:
                    x, y = self._normalized_point(selection.topLeft())
                    right, bottom = self._normalized_point(selection.bottomRight())
                    LOGGER.debug(
                        "Pad drag released rect=(%.5f,%.5f,%.5f,%.5f)",
                        x,
                        y,
                        right - x,
                        bottom - y,
                    )
                    self.pad_selected.emit(x, y, right - x, bottom - y)
                return True
            if self._click_position is not None:
                if (point - self._click_position).manhattanLength() <= 3:
                    self.pad_clicked.emit(*self._normalized_point(point))
                self._click_position = None
                return True
            if self._drag_position is not None:
                self._drag_position = None
                self.unsetCursor()
                return True
        return super().eventFilter(watched, event)

    def _ruler_measurement_mm(self) -> float:
        """Return the current ruler distance in real millimeters."""
        if self._ruler_start is None or self._ruler_end is None:
            return 0.0
        width = max(1, self._pixmap.width() - 1)
        height = max(1, self._pixmap.height() - 1)
        start = QPointF(self._ruler_start[0] * width, self._ruler_start[1] * height)
        end = QPointF(self._ruler_end[0] * width, self._ruler_end[1] * height)
        return QLineF(start, end).length() / self._ruler_pixels_per_mm

    def _pad_at_point(self, point: QPoint | QPointF) -> Pad | None:
        """Return the visible pad under a displayed-image coordinate."""
        pixel_point = point.toPoint() if isinstance(point, QPointF) else point
        if not self._label.rect().contains(pixel_point):
            return None
        x, y = self._normalized_point(pixel_point)
        image_width = max(1, self._label.width() - 1)
        image_height = max(1, self._label.height() - 1)
        for pad in reversed(self._pad_labels):
            center_x = pad.x + pad.width / 2
            center_y = pad.y + pad.height / 2
            cosine = math.cos(math.radians(-pad.rotation))
            sine = math.sin(math.radians(-pad.rotation))
            delta_x = (x - center_x) * image_width
            delta_y = (y - center_y) * image_height
            local_x = delta_x * cosine - delta_y * sine
            local_y = delta_x * sine + delta_y * cosine
            width = max(1e-9, pad.width * image_width)
            height = max(1e-9, pad.height * image_height)
            if pad.shape in {"circle", "oval"}:
                hit = (local_x / (width / 2)) ** 2 + (
                    local_y / (height / 2)
                ) ** 2 <= 1.0
            else:
                hit = abs(local_x) <= width / 2 and abs(local_y) <= height / 2
            if hit:
                return pad
        return None

    def _trace_at_point(self, point: QPoint | QPointF) -> tuple[str, str] | None:
        """Return origin/target pad IDs when point touches a visible trace."""
        if not self._connection_net or self._pad_at_point(point) is not None:
            return None
        pads = [pad for pad in self._pad_labels if pad.net == self._connection_net]
        if self._connection_trace_pairs is not None:
            visible_ids = {
                pad_id for pair in self._connection_trace_pairs for pad_id in pair
            }
            pads = [pad for pad in pads if pad.pad_id in visible_ids]
        if len(pads) < 2:
            return None
        centers = {
            pad.pad_id: QPointF(
                (pad.x + pad.width / 2) * (self._label.width() - 1),
                (pad.y + pad.height / 2) * (self._label.height() - 1),
            )
            for pad in pads
        }
        origin_id = (
            self._connection_origin_id
            if self._connection_origin_id in centers
            else pads[0].pad_id
        )
        origin = centers[origin_id]
        tolerance = max(6.0, min(self._label.width(), self._label.height()) * 0.012)
        if self._connection_trace_pairs is not None:
            for first_id, second_id in self._connection_trace_pairs:
                if first_id not in centers or second_id not in centers:
                    continue
                if (
                    self._distance_to_segment(
                        point, centers[first_id], centers[second_id]
                    )
                    <= tolerance
                ):
                    return first_id, second_id
            return None
        for pad in pads:
            if pad.pad_id == origin_id:
                continue
            target = centers[pad.pad_id]
            if self._distance_to_segment(point, origin, target) <= tolerance:
                return origin_id, pad.pad_id
        return None

    @staticmethod
    def _distance_to_segment(point: QPointF, start: QPointF, end: QPointF) -> float:
        """Return shortest distance from point to a finite line segment."""
        delta_x = end.x() - start.x()
        delta_y = end.y() - start.y()
        length_squared = delta_x * delta_x + delta_y * delta_y
        if length_squared == 0:
            return math.hypot(point.x() - start.x(), point.y() - start.y())
        projection = max(
            0.0,
            min(
                1.0,
                ((point.x() - start.x()) * delta_x + (point.y() - start.y()) * delta_y)
                / length_squared,
            ),
        )
        nearest_x = start.x() + projection * delta_x
        nearest_y = start.y() + projection * delta_y
        return math.hypot(point.x() - nearest_x, point.y() - nearest_y)

    def set_pad_placement(self, enabled: bool) -> None:
        """Enable or disable click-to-place mode for a pad."""
        if enabled:
            self.clear_device_placement()
            cursor = QCursor(Qt.CursorShape.CrossCursor)
            self.setCursor(cursor)
            self._label.setCursor(cursor)
        self._pad_placement = enabled
        if not enabled:
            self._pad_start = None
            self._pad_band.hide()
            self._click_position = None
            self._drag_position = None
            self.unsetCursor()
            self._label.unsetCursor()

    def set_delete_mode(self, enabled: bool) -> None:
        """Enable continuous deletion clicks on this board view."""
        self._delete_mode = enabled
        if enabled:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
            self._label.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        else:
            self.unsetCursor()
            self._label.unsetCursor()

    def set_device_placement(
        self,
        footprint: Footprint,
        pixels_per_mm: float,
        side: str,
        rotation: float,
    ) -> None:
        """Enable click placement with a physically scaled footprint preview."""
        self.set_pad_placement(False)
        self._device_placement = True
        self._device_preview_point = (0.5, 0.5)
        self._device_preview.configure(
            footprint,
            pixels_per_mm,
            self._fit_scale() * self._scale,
            side,
            rotation,
        )
        self._position_device_preview()
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    def set_device_rotation(self, rotation: float) -> None:
        """Update the active footprint preview rotation."""
        if self._device_placement:
            self._device_preview.set_rotation(rotation)

    def clear_device_placement(self) -> None:
        """Disable footprint placement and hide its preview."""
        self._device_placement = False
        self._device_preview.hide()
        self.unsetCursor()

    def _position_device_preview(self) -> None:
        center = QPoint(
            round(self._device_preview_point[0] * max(1, self._label.width() - 1)),
            round(self._device_preview_point[1] * max(1, self._label.height() - 1)),
        )
        self._device_preview.move(
            center.x() - self._device_preview.width() // 2,
            center.y() - self._device_preview.height() // 2,
        )

    def _label_point(self, watched, point: QPoint) -> QPoint:
        """Convert an event point to label coordinates."""
        if watched is self.viewport():
            return self._label.mapFrom(self.viewport(), point)
        return point

    def _normalized_point(self, point: QPoint) -> tuple[float, float]:
        """Convert label coordinates to normalized image coordinates."""
        return (
            max(0.0, min(1.0, point.x() / max(1, self._label.width() - 1))),
            max(0.0, min(1.0, point.y() / max(1, self._label.height() - 1))),
        )

    @staticmethod
    def _clamp_point(point: QPoint, bounds: QRect) -> QPoint:
        """Constrain a point to the image label."""
        return QPoint(
            max(bounds.left(), min(point.x(), bounds.right())),
            max(bounds.top(), min(point.y(), bounds.bottom())),
        )

    def has_image(self) -> bool:
        """Return whether this view currently contains a decoded image."""
        return not self._pixmap.isNull()

    def zoom_ratio(self) -> float:
        """Return zoom relative to the minimum full-image view."""
        return self._scale

    def _zoom_by(self, factor: float, anchor: QPoint | None = None) -> None:
        """Zoom around the viewport center without shifting the image."""
        if self._pixmap.isNull():
            return
        self._zoom_revision += 1
        revision = self._zoom_revision
        anchor = QPoint(self.viewport().width() // 2, self.viewport().height() // 2)
        old_effective = self._fit_scale() * self._scale
        old_label_point = self._label.mapFrom(self.viewport(), anchor)
        source_point = QPointF(
            old_label_point.x() / old_effective,
            old_label_point.y() / old_effective,
        )
        old_scale = self._scale
        self._scale = max(self.MIN_ZOOM, min(self._scale * factor, self.MAX_ZOOM))
        if math.isclose(old_scale, self._scale):
            return
        self._render(smooth=False)
        zoom_anchor = None
        self._restore_zoom_anchor(source_point, zoom_anchor)
        QTimer.singleShot(
            0,
            lambda: self._finish_zoom_anchor(revision, source_point, zoom_anchor),
        )
        QTimer.singleShot(
            80,
            lambda: self._finish_zoom_render(revision, source_point, zoom_anchor),
        )
        self.zoom_changed.emit(self._scale)
        self.view_changed.emit()

    def _finish_zoom_anchor(
        self, revision: int, source_point: QPointF, anchor: QPoint | None
    ) -> None:
        """Correct zoom anchoring after Qt has laid out new scrollbars."""
        if revision == self._zoom_revision:
            self._restore_zoom_anchor(source_point, anchor)

    def _restore_zoom_anchor(
        self, source_point: QPointF, anchor: QPoint | None
    ) -> None:
        """Keep one source-image point fixed in the board viewport."""
        if anchor is None:
            anchor = QPoint(
                self.viewport().width() // 2,
                self.viewport().height() // 2,
            )
        else:
            anchor = self._clamp_point(anchor, self.viewport().rect())
        new_effective = self._fit_scale() * self._scale
        new_label_point = QPoint(
            round(source_point.x() * new_effective),
            round(source_point.y() * new_effective),
        )
        current_label_point = self._label.mapFrom(self.viewport(), anchor)
        horizontal = self.horizontalScrollBar()
        vertical = self.verticalScrollBar()
        horizontal.setValue(
            horizontal.value() + new_label_point.x() - current_label_point.x()
        )
        vertical.setValue(
            vertical.value() + new_label_point.y() - current_label_point.y()
        )

    def _finish_zoom_render(
        self, revision: int, source_point: QPointF, anchor: QPoint | None
    ) -> None:
        """Replace the fast zoom preview with a smooth render when idle."""
        if revision != self._zoom_revision:
            return
        self._zoom_render_pending = False
        self._render(smooth=True)
        self._restore_zoom_anchor(source_point, anchor)

    def _render(self, smooth: bool = True) -> None:
        if self._pixmap.isNull():
            self._label.setMinimumSize(240, 180)
            self._label.setPixmap(QPixmap())
            self._label.setText(self._empty_text)
            self._device_preview.hide()
            return
        self._label.setText("")
        self._label.setMinimumSize(0, 0)
        fit_scale = self._fit_scale()
        target_size = self._pixmap.size() * (fit_scale * self._scale)
        current = self._label.pixmap()
        preview = not smooth and current is not None and not current.isNull()
        displayed = (current if preview else self._pixmap).scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            (
                Qt.TransformationMode.SmoothTransformation
                if smooth
                else Qt.TransformationMode.FastTransformation
            ),
        )
        if not preview:
            self._draw_vector_footprints(displayed)
            self._draw_vector_pads(displayed)
            self._draw_vector_pad_labels(displayed)
            self._draw_ruler(displayed)
        self._label.setPixmap(displayed)
        self._label.resize(self._label.pixmap().size())
        if self._device_placement:
            self._device_preview.set_effective_scale(fit_scale * self._scale)
            self._position_device_preview()

    def _draw_ruler(self, pixmap: QPixmap) -> None:
        """Draw the ruler line and its real-world measurement."""
        if not self._ruler_enabled or self._ruler_start is None:
            return
        width = max(1, pixmap.width() - 1)
        height = max(1, pixmap.height() - 1)
        start = QPointF(self._ruler_start[0] * width, self._ruler_start[1] * height)
        end = self._ruler_end or self._ruler_start
        end = QPointF(end[0] * width, end[1] * height)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(0, 255, 255), 3))
        painter.drawLine(start, end)
        if self._ruler_end is not None:
            label = f"{self._ruler_measurement_mm():.2f} mm"
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(
                QRectF(end.x() + 8, end.y() - 24, 150, 24),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
        painter.end()

    def _draw_vector_pad_labels(self, pixmap: QPixmap) -> None:
        """Draw labels after zoom scaling so their text stays sharp."""
        if not self._pad_labels:
            return
        painter = QPainter(pixmap)
        painter.setOpacity(1.0)
        for pad in self._pad_labels:
            width = max(2.0, pad.width * (pixmap.width() - 1))
            height = max(2.0, pad.height * (pixmap.height() - 1))
            x = pad.x * (pixmap.width() - 1)
            if self._mirror_pad_labels:
                x = (1.0 - pad.x - pad.width) * (pixmap.width() - 1)
            center = QPointF(
                x + width / 2,
                pad.y * (pixmap.height() - 1) + height / 2,
            )
            label_angle = pad.rotation + (90.0 if width < height else 0.0)
            label_angle %= 360.0
            if 90.0 < label_angle < 270.0:
                label_angle -= 180.0
            painter.save()
            painter.translate(center)
            painter.rotate(-label_angle if self._mirror_pad_labels else label_angle)
            label_width = max(width, height)
            label_height = min(width, height)
            font = painter.font()
            font.setPixelSize(max(7, min(14, round(label_height * 0.65))))
            painter.setFont(font)
            while (
                painter.fontMetrics().horizontalAdvance(pad.name) > label_width - 4
                and font.pixelSize() > 1
            ):
                font.setPixelSize(font.pixelSize() - 1)
                painter.setFont(font)
            painter.setPen(
                QColor(20, 20, 20)
                if pad.device_id is not None and pad.number == "1"
                else Qt.GlobalColor.yellow
            )
            painter.drawText(
                QRectF(
                    -label_width / 2,
                    -label_height / 2,
                    label_width,
                    label_height,
                ),
                Qt.AlignmentFlag.AlignCenter,
                pad.name,
            )
            painter.restore()
        painter.end()

    def _draw_vector_footprints(self, pixmap: QPixmap) -> None:
        """Draw placed footprint geometry after image scaling."""
        if not self._footprint_overlays:
            return
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for (
            footprint,
            x,
            y,
            rotation,
            pixels_per_mm,
            side,
            source_width,
        ) in self._footprint_overlays:
            painter.save()
            painter.translate(x * (pixmap.width() - 1), y * (pixmap.height() - 1))
            display_scale = pixmap.width() / max(1, source_width)
            _paint_footprint(
                painter,
                footprint,
                pixels_per_mm * display_scale,
                side,
                rotation,
            )
            painter.restore()
        painter.end()

    def _draw_vector_pads(self, pixmap: QPixmap) -> None:
        """Draw pad rectangles and selected-net traces after image scaling."""
        if not self._pad_labels:
            return
        painter = QPainter(pixmap)
        pads = self._pad_labels
        if (
            self._connection_preview_origin is not None
            and self._connection_preview_cursor is not None
        ):
            width = pixmap.width() - 1
            height = pixmap.height() - 1
            origin = QPointF(
                self._connection_preview_origin[0] * width,
                self._connection_preview_origin[1] * height,
            )
            cursor = QPointF(
                self._connection_preview_cursor[0] * width,
                self._connection_preview_cursor[1] * height,
            )
            preview_pen = QPen(QColor("#66c2ff"), 4.0, Qt.PenStyle.DashLine)
            preview_pen.setCosmetic(True)
            painter.setPen(preview_pen)
            painter.drawLine(origin, cursor)
        if self._connection_net:
            connected = [pad for pad in pads if pad.net == self._connection_net]
            if self._connection_trace_pairs is not None:
                visible_ids = {
                    pad_id for pair in self._connection_trace_pairs for pad_id in pair
                }
                connected = [pad for pad in connected if pad.pad_id in visible_ids]
            centers = {
                pad.pad_id: QPointF(
                    (pad.x + pad.width / 2) * (pixmap.width() - 1),
                    (pad.y + pad.height / 2) * (pixmap.height() - 1),
                )
                for pad in connected
            }
            origin_id = (
                self._connection_origin_id
                if self._connection_origin_id in centers
                else next(iter(centers), None)
            )
            if origin_id is not None:
                connection_pen = QPen(Qt.GlobalColor.white, self._CONNECTION_LINE_WIDTH)
                connection_pen.setCosmetic(True)
                painter.setPen(connection_pen)
                if self._connection_trace_pairs is not None:
                    for first_id, second_id in self._connection_trace_pairs:
                        if first_id in centers and second_id in centers:
                            painter.drawLine(centers[first_id], centers[second_id])
                elif len(connected) == 1:
                    center = centers[origin_id]
                    radius = max(8.0, min(pixmap.width(), pixmap.height()) * 0.008)
                    painter.drawEllipse(center, radius, radius)
                    painter.drawText(center + QPointF(10, -10), self._connection_net)
                elif self._connection_trace_pairs is None:
                    for pad_id, target in centers.items():
                        if pad_id != origin_id:
                            painter.drawLine(centers[origin_id], target)
        painter.setOpacity(0.45)
        for pad in pads:
            width = max(2, round(pad.width * (pixmap.width() - 1)))
            height = max(2, round(pad.height * (pixmap.height() - 1)))
            center = QPointF(
                (pad.x + pad.width / 2) * (pixmap.width() - 1),
                (pad.y + pad.height / 2) * (pixmap.height() - 1),
            )
            painter.setBrush(
                QColor("#3b82f6")
                if pad.net
                else (
                    Qt.GlobalColor.yellow
                    if pad.device_id is not None and pad.number == "1"
                    else Qt.GlobalColor.red
                )
            )
            highlighted = (
                self._connection_highlight_ids is not None
                and pad.pad_id in self._connection_highlight_ids
            )
            painter.setOpacity(1.0 if highlighted else 0.45)
            if highlighted:
                painter.setBrush(QColor("#00ff00"))
            pad_pen = QPen(
                (
                    Qt.GlobalColor.white
                    if pad.pad_id == self._connection_origin_id
                    else (QColor("#bbf7d0") if highlighted else Qt.GlobalColor.yellow)
                ),
                2.5
                if pad.pad_id == self._connection_origin_id
                else (2.0 if highlighted else 1.0),
            )
            pad_pen.setCosmetic(True)
            painter.setPen(pad_pen)
            painter.save()
            painter.translate(center)
            painter.rotate(pad.rotation)
            rectangle = QRectF(-width / 2, -height / 2, width, height)
            if pad.shape in {"circle", "oval"}:
                painter.drawEllipse(rectangle)
            elif pad.shape == "roundrect":
                painter.drawRoundedRect(rectangle, 20, 20, Qt.SizeMode.RelativeSize)
            else:
                painter.drawRect(rectangle)
            painter.restore()
        painter.end()

    def resizeEvent(self, event) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Keep the image fitted after resizing its view."""
        self._render()
        super().resizeEvent(event)

    def fit_image(self) -> None:
        """Fit image inside current view."""
        self._zoom_revision += 1
        self._zoom_render_pending = False
        self._scale = 1.0
        self._render()
        self.zoom_changed.emit(self._scale)
        self.view_changed.emit()

    def actual_size(self) -> None:
        """Show image at 1:1 source-pixel scale."""
        self._zoom_revision += 1
        self._zoom_render_pending = False
        fit_scale = self._fit_scale()
        self._scale = max(
            self.MIN_ZOOM,
            min(1.0 / fit_scale if fit_scale else 1.0, self.MAX_ZOOM),
        )
        self._render()
        self.zoom_changed.emit(self._scale)
        self.view_changed.emit()

    def center_image(self) -> None:
        """Center current image in available scrollable area."""
        horizontal = self.horizontalScrollBar()
        vertical = self.verticalScrollBar()
        horizontal.setValue((horizontal.maximum() + horizontal.minimum()) // 2)
        vertical.setValue((vertical.maximum() + vertical.minimum()) // 2)

    def view_state(self) -> tuple[float, float, float]:
        """Return zoom and normalized horizontal/vertical pan."""
        horizontal = self.horizontalScrollBar()
        vertical = self.verticalScrollBar()
        return (
            self._scale,
            horizontal.value() / horizontal.maximum() if horizontal.maximum() else 0.5,
            vertical.value() / vertical.maximum() if vertical.maximum() else 0.5,
        )

    def defer_view_state(self, state: tuple[float, float, float]) -> None:
        """Remember synchronized state without rendering a hidden board view."""
        self._pending_view_state = state

    def apply_view_state(self, state: tuple[float, float, float]) -> None:
        """Apply zoom and normalized pan from another board-side view."""
        self._pending_view_state = None
        scale = max(self.MIN_ZOOM, min(state[0], self.MAX_ZOOM))
        if not math.isclose(scale, self._scale):
            self._zoom_revision += 1
            self._scale = scale
            self._render()
            self.zoom_changed.emit(self._scale)
        horizontal = self.horizontalScrollBar()
        vertical = self.verticalScrollBar()
        horizontal.setValue(round(state[1] * horizontal.maximum()))
        vertical.setValue(round(state[2] * vertical.maximum()))

    def showEvent(self, event) -> None:  # noqa: N802
        """Render the latest synchronized state when a hidden view becomes visible."""
        super().showEvent(event)
        if self._pending_view_state is not None:
            self.apply_view_state(self._pending_view_state)

    def _fit_scale(self) -> float:
        """Calculate scale needed to fit image in viewport."""
        if self._pixmap.isNull():
            return 1.0
        # Keep FIT independent from scrollbar visibility. Otherwise the first
        # zoom step changes its own baseline and makes image/footprint zoom drift.
        viewport = self.maximumViewportSize()
        width_ratio = viewport.width() / self._pixmap.width()
        height_ratio = viewport.height() / self._pixmap.height()
        return min(1.0, width_ratio, height_ratio)
