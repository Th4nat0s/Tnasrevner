"""Basic desktop GUI for creating projects and displaying board pictures."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,invalid-name
# pylint: disable=too-many-lines

from dataclasses import dataclass, replace
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import re
import sys
from time import monotonic
from uuid import uuid4

from PySide6.QtCore import (
    QBuffer,
    QEvent,
    QIODevice,
    QLineF,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
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
    QPainter,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QMenu,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QPushButton,
    QProgressDialog,
    QRubberBand,
    QVBoxLayout,
    QWidget,
)

from .kicad import (
    CacheResult,
    Footprint,
    FootprintReference,
    KiCadCacheError,
    KiCadFootprintCache,
    KiCadFormatError,
    place_footprint_pads,
    parse_footprint,
)
from .project import (
    Device,
    ImageAsset,
    Pad,
    ProjectDocument,
    ProjectFormatError,
    ProjectStore,
)

LOGGER = logging.getLogger("tnasrevner")
_LOG_PATH: Path | None = None
_DEVICE_REFERENCE = re.compile(r"[A-Za-z][A-Za-z0-9_.+\-]{0,63}\Z")


def _application_data_directory(create: bool = True) -> Path:
    """Return the application data directory, creating it when requested."""
    directory = Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
    )
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def _qt_message_handler(mode, _context, message) -> None:
    """Write Qt runtime messages into the application log."""
    LOGGER.warning("Qt[%s] %s", mode, message)


def _configure_logging() -> Path:
    """Configure rotating application and Qt diagnostics."""
    global _LOG_PATH  # pylint: disable=global-statement
    if _LOG_PATH is not None:
        return _LOG_PATH
    directory = _application_data_directory()
    _LOG_PATH = directory / "tnasrevner.log"
    handler = RotatingFileHandler(
        _LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.addHandler(handler)
    LOGGER.propagate = False

    qInstallMessageHandler(_qt_message_handler)
    previous_hook = sys.excepthook

    def exception_hook(exception_type, value, traceback) -> None:
        LOGGER.critical(
            "Unhandled exception",
            exc_info=(exception_type, value, traceback),
        )
        previous_hook(exception_type, value, traceback)

    sys.excepthook = exception_hook
    LOGGER.info("Tnasrevner logging started: %s", _LOG_PATH)
    print(f"Tnasrevner log: {_LOG_PATH}", file=sys.stderr, flush=True)
    return _LOG_PATH


class ProjectDetailsDialog(QDialog):  # pylint: disable=too-few-public-methods
    """Collect the names needed to create a project."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New project")
        self.project_name = QLineEdit()
        self.board_name = QLineEdit()
        form = QFormLayout(self)
        form.addRow("Project name", self.project_name)
        form.addRow("Board name", self.board_name)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self) -> None:
        """Reject empty names before closing the dialog."""
        if not self.project_name.text().strip() or not self.board_name.text().strip():
            QMessageBox.warning(self, "Missing name", "Enter project and board names.")
            return
        super().accept()


class StartupDialog(QMessageBox):  # pylint: disable=too-few-public-methods
    """Ask whether to load an existing project or create a new one."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Tnasrevner")
        self.setText("What do you want to do?")
        self.load_button = self.addButton(
            "Load project", QMessageBox.ButtonRole.AcceptRole
        )
        self.new_button = self.addButton(
            "New project", QMessageBox.ButtonRole.AcceptRole
        )
        self.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)

    def choice(self) -> str | None:
        """Return `load`, `new`, or `None` after dialog closes."""
        self.exec()
        if self.clickedButton() is self.load_button:
            return "load"
        if self.clickedButton() is self.new_button:
            return "new"
        return None


class ImageEditDialog(  # pylint: disable=too-many-instance-attributes,too-many-statements,too-many-return-statements,too-many-branches
    QDialog
):
    """Rotate and crop an image before it enters a project archive."""

    def __init__(
        self, image: QPixmap, parent: QWidget | None = None
    ) -> None:  # pylint: disable=too-many-statements
        super().__init__(parent)
        self.setWindowTitle("Edit imported image")
        self.resize(1000, 700)
        self._base_image = image
        self._source = image
        self._angle = 0.0
        self._zoom = 1.0
        self._display_scale = 1.0
        self._selection_start: QPoint | None = None
        self._selection = None
        self._resize_edges: set[str] = set()
        self._calibration_start: QPoint | None = None
        self._calibration_end: QPoint | None = None
        self._calibration_line: tuple[QPointF, QPointF] | None = None
        self._result_calibration_line: tuple[float, float, float, float] | None = None
        self._edit_mode = "calibration"
        self._canvas = QLabel()
        self._canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._canvas.installEventFilter(self)
        self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self._canvas)
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._canvas)
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        calibration_button = QPushButton("Scale line")
        calibration_button.setCheckable(True)
        calibration_button.setChecked(True)
        calibration_button.clicked.connect(lambda: self._set_edit_mode("calibration"))
        crop_button = QPushButton("Crop rectangle")
        crop_button.setCheckable(True)
        crop_button.clicked.connect(lambda: self._set_edit_mode("crop"))
        self._calibration_button = calibration_button
        self._crop_button = crop_button
        millimeters = QDoubleSpinBox()
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
        controls = QHBoxLayout()
        controls.addWidget(calibration_button)
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
                "Draw scale line, enter real length in mm, then select crop rectangle."
            )
        )
        layout.addWidget(self._scroll)
        layout.addLayout(controls)
        self.showMaximized()
        self._render()
        QTimer.singleShot(0, self._render)

    def prepare_existing_image(
        self,
        pixels_per_mm: float,
        calibration_line: tuple[float, float, float, float] | None = None,
    ) -> None:
        """Start editing with the existing crop and scale still valid."""
        if pixels_per_mm <= 0:
            return
        width = max(2, self._canvas.width() - 2)
        height = max(2, self._canvas.height() - 2)
        self._set_selection(QPoint(1, 1), QPoint(width, height))
        if calibration_line is None:
            start = QPoint(1, max(1, height // 2))
            end = QPoint(width, max(1, height // 2))
        else:
            start = QPoint(
                round(calibration_line[0] * self._source.width() * self._display_scale),
                round(
                    calibration_line[1] * self._source.height() * self._display_scale
                ),
            )
            end = QPoint(
                round(calibration_line[2] * self._source.width() * self._display_scale),
                round(
                    calibration_line[3] * self._source.height() * self._display_scale
                ),
            )
        self._set_calibration_line(start, end)
        self._millimeters.setValue(QLineF(start, end).length() / pixels_per_mm)
        self._update_confirm_state()

    def eventFilter(
        self, watched, event
    ) -> bool:  # noqa: N802  # pylint: disable=too-many-return-statements
        """Track rectangle selection on the image canvas."""
        if watched is not self._canvas:
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.Wheel:
            anchor = self._canvas.mapTo(
                self._scroll.viewport(), event.position().toPoint()
            )
            self._zoom_by(
                1.2 if event.angleDelta().y() > 0 else 1 / 1.2,
                anchor,
            )
            return True
        if (
            event.type() == QEvent.Type.NativeGesture
            and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture
        ):
            self._zoom_by(max(0.01, 1.0 + event.value()))
            return True
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
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            position = event.position().toPoint()
            self._resize_edges = self._hit_edges(position)
            self._selection_start = position
            if not self._resize_edges:
                self._selection = None
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

    def accept(self) -> None:
        """Crop selected area and close editor."""
        if (
            self._selection is None
            or self._selection.width() < 2
            or self._selection.height() < 2
            or self._calibration_line is None
            or self._millimeters.value() <= 0
        ):
            QMessageBox.warning(
                self,
                "Incomplete import",
                "Define scale line, real length, and crop rectangle first.",
            )
            return
        selection = self._selection
        source_rect = self._source_rect(selection)
        if self._calibration_line is not None:
            start, end = self._calibration_line
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
        if self._calibration_line is None or self._millimeters.value() <= 0:
            return 0.0
        return QLineF(*self._calibration_line).length() / self._millimeters.value()

    def calibration_line(self) -> tuple[float, float, float, float] | None:
        """Return accepted scale-line coordinates normalized to output image."""
        return self._result_calibration_line

    def _set_edit_mode(self, mode: str) -> None:
        """Switch between scale-line and crop-rectangle drawing."""
        self._edit_mode = mode
        self._calibration_button.setChecked(mode == "calibration")
        self._crop_button.setChecked(mode == "crop")

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
            and self._calibration_line is not None
            and self._millimeters.value() > 0
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(enabled)

    def _rotate(self, angle: int) -> None:
        """Rotate source image and clear old selection."""
        self._set_angle(self._angle + angle)

    def _set_angle(self, angle: float) -> None:
        """Apply free rotation relative to original imported image."""
        old_size = self._source.size()
        selection_ratio = None
        if self._selection is not None:
            selection = self._source_rect(self._selection)
            selection_ratio = (
                selection.x() / old_size.width(),
                selection.y() / old_size.height(),
                selection.width() / old_size.width(),
                selection.height() / old_size.height(),
            )
        line_ratio = None
        if self._calibration_line is not None:
            line_ratio = tuple(
                QPointF(point.x() / old_size.width(), point.y() / old_size.height())
                for point in self._calibration_line
            )
        self._angle = angle
        self._angle_spin.blockSignals(True)
        self._angle_spin.setValue(angle)
        self._angle_spin.blockSignals(False)
        self._source = self._base_image.transformed(
            QTransform().rotate(angle), Qt.TransformationMode.SmoothTransformation
        )
        new_size = self._source.size()
        if line_ratio is not None:
            self._calibration_line = tuple(
                QPointF(point.x() * new_size.width(), point.y() * new_size.height())
                for point in line_ratio
            )
        self._update_confirm_state()
        self._render()
        if selection_ratio is not None:
            selection = QRect(
                round(selection_ratio[0] * new_size.width() * self._display_scale),
                round(selection_ratio[1] * new_size.height() * self._display_scale),
                round(selection_ratio[2] * new_size.width() * self._display_scale),
                round(selection_ratio[3] * new_size.height() * self._display_scale),
            ).intersected(self._canvas.rect())
            if selection.width() >= 2 and selection.height() >= 2:
                self._update_selection(selection)

    def _zoom_by(self, factor: float, anchor: QPoint | None = None) -> None:
        """Zoom around anchor point, preserving content under cursor."""
        selection_ratio = self._selection_ratio()
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
        old_horizontal = self._scroll.horizontalScrollBar().value()
        old_vertical = self._scroll.verticalScrollBar().value()
        self._zoom = max(0.1, min(self._zoom * factor, 20.0))
        self._render()
        self._restore_selection(selection_ratio)
        new_canvas_point = QPoint(
            round(source_point.x() * self._display_scale),
            round(source_point.y() * self._display_scale),
        )
        self._scroll.horizontalScrollBar().setValue(
            old_horizontal + new_canvas_point.x() - old_canvas_point.x()
        )
        self._scroll.verticalScrollBar().setValue(
            old_vertical + new_canvas_point.y() - old_canvas_point.y()
        )

    def _fit_view(self) -> None:
        """Reset editor preview zoom to fit."""
        selection_ratio = self._selection_ratio()
        self._zoom = 1.0
        self._render()
        self._restore_selection(selection_ratio)

    def _render(self) -> None:
        """Fit source image to editor viewport."""
        viewport = self._scroll.viewport().size()
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
        painter.end()
        self._canvas.setPixmap(displayed)
        self._canvas.resize(displayed.size())

    def _set_selection(self, start: QPoint, end: QPoint) -> None:
        """Update selection rectangle constrained to displayed image."""
        bounds = self._canvas.rect()
        start = self._clamp_point(start, bounds)
        end = self._clamp_point(end, bounds)
        self._update_selection(QRect(start, end).normalized())

    def _selection_ratio(self) -> tuple[float, float, float, float] | None:
        """Return selection bounds as ratios of the source image."""
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
            self._update_selection(selection)

    def _update_selection(self, selection: QRect) -> None:
        """Apply selection geometry and update validation state."""
        self._selection = selection
        self._rubber_band.setGeometry(self._selection)
        self._rubber_band.show()
        self._update_confirm_state()

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


class FootprintPickerDialog(QDialog):  # pylint: disable=too-few-public-methods
    """Search and select one cached KiCad footprint."""

    _MAX_RESULTS = 750

    def __init__(
        self,
        references: tuple[FootprintReference, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select KiCad footprint")
        self.resize(680, 520)
        self._references = references
        self._visible_references: list[FootprintReference] = []
        self._search = QLineEdit(self)
        self._search.setPlaceholderText("Search library or footprint name…")
        self._list = QListWidget(self)
        self._status = QLabel(self)
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        layout = QVBoxLayout(self)
        layout.addWidget(self._search)
        layout.addWidget(self._list)
        layout.addWidget(self._status)
        layout.addWidget(self._buttons)
        self._search.textChanged.connect(self._refresh_results)
        self._list.currentRowChanged.connect(
            lambda row: self._buttons.button(
                QDialogButtonBox.StandardButton.Ok
            ).setEnabled(row >= 0)
        )
        self._list.itemDoubleClicked.connect(lambda _item: self.accept())
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._refresh_results("")
        self._search.setFocus()

    def selected_reference(self) -> FootprintReference | None:
        """Return the selected cached footprint reference."""
        row = self._list.currentRow()
        return self._visible_references[row] if row >= 0 else None

    def _refresh_results(self, query: str) -> None:
        words = query.casefold().split()
        matches = [
            reference
            for reference in self._references
            if all(word in reference.identifier.casefold() for word in words)
        ]
        self._visible_references = matches[: self._MAX_RESULTS]
        self._list.clear()
        for reference in self._visible_references:
            self._list.addItem(QListWidgetItem(reference.identifier))
        shown = len(self._visible_references)
        self._status.setText(f"{shown} shown / {len(matches)} matches")
        if shown:
            self._list.setCurrentRow(0)


class KiCadCacheWorker(QObject):  # pylint: disable=too-few-public-methods
    """Prepare the footprint cache away from the GUI event loop."""

    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, cache: KiCadFootprintCache) -> None:
        super().__init__()
        self._cache = cache

    @Slot()
    def run(self) -> None:
        """Run one cache update and report its result."""
        try:
            self.completed.emit(self._cache.ensure_ready())
        except KiCadCacheError as error:
            self.failed.emit(str(error))


@dataclass(frozen=True)
class PendingDevice:
    """Footprint and metadata waiting for a click on a board image."""

    reference: str
    source: FootprintReference
    footprint: Footprint
    content: bytes
    revision: str
    rotation: float = 0.0


class FootprintPreview(QWidget):  # pylint: disable=too-few-public-methods
    """Transparent footprint preview following the placement pointer."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._footprint: Footprint | None = None
        self._pixels_per_mm = 1.0
        self._effective_scale = 1.0
        self._side = "top"
        self._rotation = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()

    def configure(
        self,
        footprint: Footprint,
        pixels_per_mm: float,
        effective_scale: float,
        side: str,
        rotation: float,
    ) -> None:
        """Configure geometry and physical display scale."""
        self._footprint = footprint
        self._pixels_per_mm = pixels_per_mm
        self._effective_scale = effective_scale
        self._side = side
        self._rotation = rotation
        self._resize_for_footprint()
        self.show()
        self.raise_()
        self.update()

    def set_effective_scale(self, effective_scale: float) -> None:
        """Follow image zoom without changing the footprint's real scale."""
        self._effective_scale = effective_scale
        self._resize_for_footprint()
        self.update()

    def set_rotation(self, rotation: float) -> None:
        """Rotate the preview around its footprint anchor."""
        self._rotation = rotation
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        """Draw footprint graphics and pads over the image."""
        del event
        if self._footprint is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(self.width() / 2, self.height() / 2)
        _paint_footprint(
            painter,
            self._footprint,
            self._pixels_per_mm * self._effective_scale,
            self._side,
            self._rotation,
            preview=True,
        )
        painter.end()

    def _resize_for_footprint(self) -> None:
        if self._footprint is None:
            return
        radius = self._footprint.radius() * self._pixels_per_mm * self._effective_scale
        diameter = max(24, min(5000, math.ceil(radius * 2 + 20)))
        center = self.geometry().center()
        self.resize(diameter, diameter)
        self.move(center.x() - diameter // 2, center.y() - diameter // 2)


def _paint_footprint(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    painter: QPainter,
    footprint: Footprint,
    pixels_per_mm: float,
    side: str,
    rotation: float,
    preview: bool = False,
) -> None:
    """Draw parsed footprint geometry around the painter's current origin."""
    painter.save()
    painter.rotate(rotation)
    painter.scale(-pixels_per_mm if side == "bottom" else pixels_per_mm, pixels_per_mm)
    pad_pen = QPen(QColor(255, 225, 0, 230 if preview else 180), 1.5)
    pad_pen.setCosmetic(True)
    painter.setPen(pad_pen)
    painter.setBrush(QColor(255, 80, 40, 120 if preview else 70))
    for pad in footprint.pads:
        painter.save()
        painter.translate(pad.x, pad.y)
        painter.rotate(pad.rotation)
        rectangle = QRectF(-pad.width / 2, -pad.height / 2, pad.width, pad.height)
        if pad.shape in {"circle", "oval"}:
            painter.drawEllipse(rectangle)
        elif pad.shape == "roundrect":
            painter.drawRoundedRect(rectangle, 20, 20, Qt.SizeMode.RelativeSize)
        else:
            painter.drawRect(rectangle)
        painter.restore()
    outline_pen = QPen(QColor(0, 255, 255, 240 if preview else 180), 1.5)
    outline_pen.setCosmetic(True)
    painter.setPen(outline_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for graphic in footprint.graphics:
        coordinates = graphic.coordinates
        if graphic.kind == "line":
            painter.drawLine(QLineF(*coordinates))
        elif graphic.kind == "rect":
            painter.drawRect(
                QRectF(
                    QPointF(coordinates[0], coordinates[1]),
                    QPointF(coordinates[2], coordinates[3]),
                ).normalized()
            )
        elif graphic.kind == "circle":
            radius = math.hypot(
                coordinates[2] - coordinates[0], coordinates[3] - coordinates[1]
            )
            painter.drawEllipse(QPointF(coordinates[0], coordinates[1]), radius, radius)
    painter.restore()


class ImageView(QScrollArea):  # pylint: disable=too-many-instance-attributes
    """Scrollable image view with mouse-wheel zoom."""

    view_changed = Signal()
    pad_selected = Signal(float, float, float, float)
    pad_clicked = Signal(float, float)
    pad_context_requested = Signal(float, float)
    pad_menu_requested = Signal(float, float)
    device_placed = Signal(float, float)
    device_rotated = Signal()

    def __init__(self, empty_text: str) -> None:
        super().__init__()
        self._empty_text = empty_text
        self._pixmap = QPixmap()
        self._scale = 1.0
        self._drag_position: QPoint | None = None
        self._pad_placement = False
        self._pad_start: QPoint | None = None
        self._click_position: QPoint | None = None
        self._device_placement = False
        self._device_preview_point = (0.5, 0.5)
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
        self._pixmap = QPixmap(str(path)) if path else QPixmap()
        self._scale = 1.0
        self._render()

    def set_image_data(self, content: bytes) -> None:
        """Display image bytes loaded from a `.revp` archive."""
        self._pixmap = QPixmap()
        self._pixmap.loadFromData(content)
        self._scale = 1.0
        self._render()

    def set_pixmap(self, pixmap: QPixmap) -> None:
        """Display an already composed pixmap."""
        self._pixmap = pixmap
        self._scale = 1.0
        self._render()

    def wheelEvent(self, event) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Zoom image with Ctrl+wheel, preserving normal scroll behavior."""
        if (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and not self._pixmap.isNull()
        ):
            anchor = self.viewport().mapFrom(self, event.position().toPoint())
            self._zoom_by(
                1.2 if event.angleDelta().y() > 0 else 1 / 1.2,
                anchor,
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
        if watched not in (self._label, self.viewport()):
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.MouseButtonPress:
            point = self._label_point(watched, event.position().toPoint())
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
                    self.pad_context_requested.emit(*self._normalized_point(point))
                    return True
                return super().eventFilter(watched, event)
            if event.button() != Qt.MouseButton.LeftButton:
                return super().eventFilter(watched, event)
            if (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                and not self._pixmap.isNull()
                and self._label.rect().contains(point)
            ):
                self.pad_menu_requested.emit(*self._normalized_point(point))
                return True
            if self._pad_placement and not self._pixmap.isNull():
                if self._label.rect().contains(point):
                    self._pad_start = point
                    self._pad_band.setGeometry(QRect(point, point))
                    self._pad_band.show()
                    LOGGER.debug("Pad drag started point=(%s,%s)", point.x(), point.y())
                    return True
            self._click_position = point
            self._drag_position = event.globalPosition().toPoint()
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
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
            point = self._clamp_point(
                self._label_point(watched, event.position().toPoint()),
                self._label.rect(),
            )
            if self._pad_start is not None:
                selection = QRect(self._pad_start, point).normalized()
                self._pad_start = None
                self._pad_band.hide()
                self._pad_placement = False
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
            if self._drag_position is not None and self._click_position is not None:
                if (point - self._click_position).manhattanLength() <= 3:
                    self.pad_clicked.emit(*self._normalized_point(point))
                self._click_position = None
                self._drag_position = None
                self.unsetCursor()
                return True
        return super().eventFilter(watched, event)

    def set_pad_placement(self, enabled: bool) -> None:
        """Enable or disable click-to-place mode for a pad."""
        if enabled:
            self.clear_device_placement()
        self._pad_placement = enabled
        if not enabled:
            self._pad_start = None
            self._pad_band.hide()
            self._click_position = None
            self._drag_position = None
            self.unsetCursor()

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

    def _zoom_by(self, factor: float, anchor: QPoint | None = None) -> None:
        """Zoom around anchor point, preserving content under cursor."""
        if self._pixmap.isNull():
            return
        anchor = anchor or QPoint(
            self.viewport().width() // 2, self.viewport().height() // 2
        )
        old_effective = self._fit_scale() * self._scale
        old_label_point = self._label.mapFrom(self.viewport(), anchor)
        source_point = QPointF(
            old_label_point.x() / old_effective,
            old_label_point.y() / old_effective,
        )
        old_horizontal = self.horizontalScrollBar().value()
        old_vertical = self.verticalScrollBar().value()
        self._scale = max(0.1, min(self._scale * factor, 20.0))
        self._render()
        new_effective = self._fit_scale() * self._scale
        new_label_point = QPoint(
            round(source_point.x() * new_effective),
            round(source_point.y() * new_effective),
        )
        self.horizontalScrollBar().setValue(
            old_horizontal + new_label_point.x() - old_label_point.x()
        )
        self.verticalScrollBar().setValue(
            old_vertical + new_label_point.y() - old_label_point.y()
        )
        self.view_changed.emit()

    def _render(self) -> None:
        if self._pixmap.isNull():
            self._label.setPixmap(QPixmap())
            self._label.setText(self._empty_text)
            self._device_preview.hide()
            return
        self._label.setText("")
        fit_scale = self._fit_scale()
        self._label.setPixmap(
            self._pixmap.scaled(
                self._pixmap.size() * (fit_scale * self._scale),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._label.resize(self._label.pixmap().size())
        if self._device_placement:
            self._device_preview.set_effective_scale(fit_scale * self._scale)
            self._position_device_preview()

    def resizeEvent(self, event) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Keep the image fitted after resizing its view."""
        self._render()
        super().resizeEvent(event)

    def fit_image(self) -> None:
        """Fit image inside current view."""
        self._scale = 1.0
        self._render()
        self.view_changed.emit()

    def actual_size(self) -> None:
        """Show image at 1:1 source-pixel scale."""
        fit_scale = self._fit_scale()
        self._scale = 1.0 / fit_scale if fit_scale else 1.0
        self._render()
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

    def apply_view_state(self, state: tuple[float, float, float]) -> None:
        """Apply zoom and normalized pan from another board-side view."""
        self._scale = max(0.1, min(state[0], 20.0))
        self._render()
        horizontal = self.horizontalScrollBar()
        vertical = self.verticalScrollBar()
        horizontal.setValue(round(state[1] * horizontal.maximum()))
        vertical.setValue(round(state[2] * vertical.maximum()))

    def _fit_scale(self) -> float:
        """Calculate scale needed to fit image in viewport."""
        if self._pixmap.isNull():
            return 1.0
        viewport = self.viewport().size()
        width_ratio = viewport.width() / self._pixmap.width()
        height_ratio = viewport.height() / self._pixmap.height()
        return min(1.0, width_ratio, height_ratio)


class MainWindow(QMainWindow):  # pylint: disable=too-many-instance-attributes
    """Minimal project and board-picture workspace."""

    def __init__(  # pylint: disable=too-many-statements
        self,
        show_startup: bool = True,
        footprint_cache: KiCadFootprintCache | None = None,
    ) -> None:
        super().__init__()
        self.project: ProjectDocument | None = None
        self.store: ProjectStore | None = None
        self._dirty = False
        self._syncing_views = False
        self._pending_pad: Pad | None = None
        self._pending_device: PendingDevice | None = None
        self._pending_device_reference: str | None = None
        self._selected_net: str | None = None
        self._selected_pad_id: str | None = None
        self._pads_visible = True
        self._pad_refresh_pending = False
        self._pending_pad_view_state: tuple[float, float, float] | None = None
        self._image_cache: dict[str, QPixmap] = {}
        self._device_footprint_cache: dict[str, Footprint] = {}
        self._footprint_cache = footprint_cache or KiCadFootprintCache(
            _application_data_directory(create=False) / "kicad-footprints"
        )
        self._kicad_cache_thread: QThread | None = None
        self._kicad_cache_worker: KiCadCacheWorker | None = None
        self._kicad_progress: QProgressDialog | None = None
        self._kicad_revision: str | None = None
        self._close_when_cache_finishes = False
        self._net_dialog: QInputDialog | None = None
        self._pad_menu: QMenu | None = None
        self._last_view_key: int | None = None
        self._last_view_time = 0.0
        self.setWindowTitle("Tnasrevner")
        self.resize(1100, 700)
        self._views = {
            "top": ImageView("No top picture"),
            "bottom": ImageView("No bottom picture"),
        }
        self._tabs = QTabWidget()
        self._tabs.addTab(self._views["top"], "Top")
        self._tabs.addTab(self._views["bottom"], "Bottom")
        side_by_side = QWidget()
        side_layout = QHBoxLayout(side_by_side)
        self._side_views = {
            "top": ImageView("No top picture"),
            "bottom": ImageView("No bottom picture"),
        }
        side_layout.addWidget(self._side_views["top"])
        side_layout.addWidget(self._side_views["bottom"])
        self._tabs.addTab(side_by_side, "Top + bottom")
        self._overlay_view = ImageView("No top/bottom images")
        self._tabs.addTab(self._overlay_view, "Both")
        self._net_table = QTableWidget(0, 2)
        self._net_table.setHorizontalHeaderLabels(["Pad", "Net"])
        self._net_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tabs.addTab(self._net_table, "Nets")
        for view in (*self._views.values(), *self._side_views.values()):
            view.view_changed.connect(lambda view=view: self._sync_board_views(view))
        for side, view in self._views.items():
            view.pad_clicked.connect(
                lambda x, y, side=side: self._select_pad(side, x, y)
            )
            view.pad_selected.connect(
                lambda x, y, width, height, side=side: self._place_pad(
                    side, x, y, width, height
                )
            )
            view.pad_context_requested.connect(
                lambda x, y, side=side: self._defer_pad_net_assignment(side, x, y)
            )
            view.pad_menu_requested.connect(
                lambda x, y, side=side: self._defer_pad_menu(side, x, y)
            )
            view.device_placed.connect(
                lambda x, y, side=side: self._place_device(side, x, y)
            )
            view.device_rotated.connect(self._rotate_pending_device)
        for side, view in self._side_views.items():
            view.pad_selected.connect(
                lambda x, y, width, height, side=side: self._place_pad(
                    side, x, y, width, height
                )
            )
            view.pad_clicked.connect(
                lambda x, y, side=side: self._select_pad(side, x, y)
            )
            view.pad_context_requested.connect(
                lambda x, y, side=side: self._defer_pad_net_assignment(side, x, y)
            )
            view.pad_menu_requested.connect(
                lambda x, y, side=side: self._defer_pad_menu(side, x, y)
            )
            view.device_placed.connect(
                lambda x, y, side=side: self._place_device(side, x, y)
            )
            view.device_rotated.connect(self._rotate_pending_device)
        self.setCentralWidget(self._tabs)
        self._create_actions()
        self._create_tool_palette()
        self._create_view_menu()
        self._update_title()
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(5_000)
        self._heartbeat_timer.timeout.connect(self._log_ui_heartbeat)
        self._heartbeat_timer.start()
        if show_startup:
            QTimer.singleShot(0, self._startup_choice)
            QTimer.singleShot(1_000, self._prefetch_footprints)

    def _create_tool_palette(self) -> None:  # pylint: disable=too-many-statements
        """Create the right-side view control palette."""
        dock = QDockWidget("Tools", self)
        dock.setObjectName("toolsDock")
        panel = QWidget(dock)
        layout = QVBoxLayout(panel)
        actual_button = QPushButton("1:1", panel)
        actual_button.setToolTip("Actual image size")
        actual_button.clicked.connect(self._actual_size)
        layout.addWidget(actual_button)
        fit_button = QPushButton("FIT", panel)
        fit_button.setToolTip("Fit image in view")
        fit_button.clicked.connect(self._fit_images)
        layout.addWidget(fit_button)
        center_button = QPushButton("◎", panel)
        center_button.setToolTip("Center image")
        center_button.clicked.connect(self._center_images)
        layout.addWidget(center_button)
        layout.addSpacing(12)
        import_button = QPushButton("Import image", panel)
        import_button.setToolTip("Import image, then choose Top or Bottom")
        import_button.clicked.connect(self.import_picture)
        layout.addWidget(import_button)
        remove_button = QPushButton("Remove image", panel)
        remove_button.setToolTip("Remove a Top or Bottom image")
        remove_button.clicked.connect(self.remove_picture)
        layout.addWidget(remove_button)
        edit_button = QPushButton("Edit image", panel)
        edit_button.setToolTip("Adjust crop or rotate an imported image")
        edit_button.clicked.connect(self.edit_picture)
        layout.addWidget(edit_button)
        device_button = QPushButton("Add device", panel)
        device_button.setToolTip("Select and place a KiCad footprint")
        device_button.clicked.connect(self.add_device)
        layout.addWidget(device_button)
        self._add_device_button = device_button
        pad_button = QPushButton("Create pad", panel)
        pad_button.setToolTip("Create a pad on the Top or Bottom image")
        pad_button.clicked.connect(self.create_pad)
        layout.addWidget(pad_button)
        show_pads_button = QPushButton("Show pads", panel)
        show_pads_button.setCheckable(True)
        show_pads_button.setChecked(True)
        show_pads_button.setToolTip("Show or hide pad rectangles and connections")
        show_pads_button.toggled.connect(self._set_pads_visible)
        layout.addWidget(show_pads_button)
        log_button = QPushButton("Log file", panel)
        log_button.setToolTip("Show diagnostic log file path")
        log_button.clicked.connect(self._show_log_path)
        layout.addWidget(log_button)
        save_button = QPushButton("Save", panel)
        save_button.setToolTip("Save project")
        save_button.clicked.connect(self.save_project)
        layout.addWidget(save_button)
        layout.addStretch()
        panel.setLayout(layout)
        dock.setWidget(panel)
        self._tools_dock = dock
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def _create_view_menu(self) -> None:
        """Create menu actions for restoring optional panels."""
        view_menu = self.menuBar().addMenu("View")
        tools_action = self._tools_dock.toggleViewAction()
        tools_action.setText("Tools")
        view_menu.addAction(tools_action)

    def _startup_choice(self) -> None:
        """Show startup choice and open the selected workflow."""
        choice = StartupDialog(self).choice()
        if choice == "load":
            self.open_project()
        elif choice == "new":
            self.new_project()

    def _active_views(self) -> list[ImageView]:
        """Return image views belonging to current tab."""
        if self._tabs.currentIndex() == 0:
            return [self._views["top"]]
        if self._tabs.currentIndex() == 1:
            return [self._views["bottom"]]
        if self._tabs.currentIndex() == 2:
            return list(self._side_views.values())
        return [self._overlay_view]

    def _sync_board_views(self, source: ImageView) -> None:
        """Synchronize zoom and pan between Top and Bottom views."""
        if self._syncing_views:
            return
        state = source.view_state()
        self._syncing_views = True
        try:
            for view in (*self._views.values(), *self._side_views.values()):
                if view is not source:
                    view.apply_view_state(state)
        finally:
            self._syncing_views = False

    def _actual_size(self) -> None:
        """Set active image view(s) to 1:1 scale."""
        for view in self._active_views():
            view.actual_size()

    def _fit_images(self) -> None:
        """Fit active image view(s) to their available space."""
        for view in self._active_views():
            view.fit_image()

    def _center_images(self) -> None:
        """Center active image view(s)."""
        for view in self._active_views():
            view.center_image()

    def _prefetch_footprints(self) -> None:
        """Download or refresh the KiCad footprint cache after application start."""
        if not self._footprint_cache.is_ready_and_fresh():
            self._start_kicad_cache_update(interactive=False)

    def add_device(self) -> None:
        """Collect a reference, select a footprint, and arm image placement."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        if not any(image.pixels_per_mm for image in self.project.images):
            QMessageBox.information(
                self,
                "No calibrated image",
                "Import and calibrate a board image before adding a device.",
            )
            return
        reference, accepted = QInputDialog.getText(
            self, "Add device", "Reference (for example U1):"
        )
        reference = reference.strip()
        if not accepted:
            return
        if not _DEVICE_REFERENCE.fullmatch(reference):
            QMessageBox.warning(
                self,
                "Invalid reference",
                "Use 1–64 letters, digits, '.', '_', '+', or '-', starting with a letter.",
            )
            return
        if any(
            device.reference.casefold() == reference.casefold()
            for device in self.project.devices
        ):
            QMessageBox.warning(
                self, "Duplicate reference", f"Device {reference} already exists."
            )
            return
        self._pending_device_reference = reference
        if self._footprint_cache.is_ready_and_fresh():
            self._kicad_revision = self._footprint_cache.current_revision()
            self._open_footprint_picker()
        else:
            self._start_kicad_cache_update(interactive=True)

    def _start_kicad_cache_update(self, interactive: bool) -> None:
        """Start one asynchronous first-run/monthly cache operation."""
        if interactive:
            self._show_kicad_progress()
        if self._kicad_cache_thread is not None:
            return
        thread = QThread(self)
        worker = KiCadCacheWorker(self._footprint_cache)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._kicad_cache_ready)
        worker.failed.connect(self._kicad_cache_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.completed.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._kicad_cache_thread_finished)
        self._kicad_cache_thread = thread
        self._kicad_cache_worker = worker
        LOGGER.info("KiCad footprint cache update started")
        thread.start()

    def _show_kicad_progress(self) -> None:
        if self._kicad_progress is not None:
            return
        progress = QProgressDialog("Preparing KiCad footprint library…", "", 0, 0, self)
        progress.setWindowTitle("KiCad footprints")
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.WindowModality.NonModal)
        progress.setMinimumDuration(0)
        progress.show()
        self._kicad_progress = progress

    def _close_kicad_progress(self) -> None:
        if self._kicad_progress is not None:
            self._kicad_progress.close()
            self._kicad_progress.deleteLater()
            self._kicad_progress = None

    @Slot(object)
    def _kicad_cache_ready(self, result: object) -> None:
        """Continue pending device creation after cache preparation."""
        self._close_kicad_progress()
        if not isinstance(result, CacheResult):
            self._kicad_cache_failed("KiCad cache returned an invalid result.")
            return
        self._kicad_revision = result.revision
        LOGGER.info(
            "KiCad footprint cache ready revision=%s refreshed=%s warning=%s",
            result.revision,
            result.refreshed,
            result.warning,
        )
        if result.warning:
            if self._pending_device_reference:
                QMessageBox.warning(self, "KiCad cache", result.warning)
            else:
                LOGGER.warning("%s", result.warning)
        if self._pending_device_reference:
            QTimer.singleShot(0, self._open_footprint_picker)

    @Slot(str)
    def _kicad_cache_failed(self, message: str) -> None:
        """Report failure when no valid footprint cache is available."""
        self._close_kicad_progress()
        LOGGER.warning("KiCad footprint cache unavailable: %s", message)
        if self._pending_device_reference:
            QMessageBox.warning(self, "KiCad footprints unavailable", message)
            self._pending_device_reference = None

    @Slot()
    def _kicad_cache_thread_finished(self) -> None:
        """Release finished worker/thread references."""
        thread = self._kicad_cache_thread
        self._kicad_cache_thread = None
        self._kicad_cache_worker = None
        if thread is None:
            return
        thread.deleteLater()
        if self._close_when_cache_finishes:
            QTimer.singleShot(0, self.close)

    def _open_footprint_picker(self) -> None:
        reference = self._pending_device_reference
        if reference is None:
            return
        try:
            catalog = self._footprint_cache.catalog()
        except KiCadCacheError as error:
            QMessageBox.warning(self, "KiCad footprints unavailable", str(error))
            self._pending_device_reference = None
            return
        dialog = FootprintPickerDialog(catalog, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._pending_device_reference = None
            return
        source = dialog.selected_reference()
        if source is None:
            self._pending_device_reference = None
            return
        try:
            footprint, content = self._footprint_cache.load(source)
        except (KiCadCacheError, KiCadFormatError) as error:
            QMessageBox.warning(self, "Invalid footprint", str(error))
            self._pending_device_reference = None
            return
        revision = self._kicad_revision or self._footprint_cache.current_revision()
        if revision is None:
            QMessageBox.warning(
                self, "KiCad cache", "The footprint source revision is missing."
            )
            self._pending_device_reference = None
            return
        self._pending_device_reference = None
        self._begin_device_placement(reference, source, footprint, content, revision)

    def _begin_device_placement(
        self,
        reference: str,
        source: FootprintReference,
        footprint: Footprint,
        content: bytes,
        revision: str,
    ) -> None:
        """Arm every calibrated board-side view for footprint placement."""
        if not self.project:
            return
        calibrated = {
            image.side: image
            for image in self.project.images
            if image.pixels_per_mm is not None
        }
        if not calibrated:
            return
        self._cancel_device_placement(clear_status=False)
        self._pending_device = PendingDevice(
            reference, source, footprint, content, revision
        )
        if len(calibrated) == 2:
            self._tabs.setCurrentIndex(2)
            views = self._side_views
        else:
            side = next(iter(calibrated))
            self._tabs.setCurrentIndex(0 if side == "top" else 1)
            views = {side: self._views[side]}
        for side, view in views.items():
            image = calibrated.get(side)
            if image and image.pixels_per_mm is not None and view.has_image():
                view.set_device_placement(footprint, image.pixels_per_mm, side, 0.0)
        self.statusBar().showMessage(
            f"Place {reference}: left click to place, right click rotates 45°, Esc cancels."
        )
        LOGGER.info(
            "Device placement armed reference=%s footprint=%s",
            reference,
            footprint.identifier,
        )

    def _rotate_pending_device(self) -> None:
        """Rotate the pending footprint clockwise by exactly 45 degrees."""
        if self._pending_device is None:
            return
        rotation = (self._pending_device.rotation + 45.0) % 360.0
        self._pending_device = replace(self._pending_device, rotation=rotation)
        for view in (*self._views.values(), *self._side_views.values()):
            view.set_device_rotation(rotation)
        self.statusBar().showMessage(
            f"{self._pending_device.reference}: {rotation:g}° — left click to place."
        )
        LOGGER.debug("Pending device rotated angle=%s", rotation)

    def _place_device(self, side: str, x: float, y: float) -> None:
        """Persist a footprint instance and all its named pads."""
        pending = self._pending_device
        if not self.project or not self.store or pending is None:
            return
        view_state = self._active_views()[0].view_state()
        image = next(
            (
                asset
                for asset in self.project.images
                if asset.side == side and asset.pixels_per_mm is not None
            ),
            None,
        )
        pixmap = self._base_pixmap_for_asset(side)
        if image is None or image.pixels_per_mm is None or pixmap.isNull():
            QMessageBox.warning(
                self, "Cannot place device", "The selected image is not calibrated."
            )
            return
        try:
            placed_pads = place_footprint_pads(
                pending.footprint,
                side,
                x,
                y,
                pending.rotation,
                pixmap.width(),
                pixmap.height(),
                image.pixels_per_mm,
            )
            device_id = str(uuid4())
            device = Device(
                pending.reference,
                side,
                x,
                y,
                pending.footprint.library,
                pending.footprint.name,
                f"assets/kicad/{device_id}.kicad_mod",
                pending.revision,
                device_id=device_id,
                rotation=pending.rotation,
            )
            generated = [
                Pad(
                    f"{pending.reference}.{placed.number}",
                    side,
                    placed.x,
                    placed.y,
                    width=placed.width,
                    height=placed.height,
                    device_id=device.device_id,
                    number=placed.number,
                    shape=placed.shape,
                    rotation=placed.rotation,
                )
                for placed in placed_pads
            ]
            existing_names = {pad.name for pad in self.project.pads}
            if any(pad.name in existing_names for pad in generated):
                raise ProjectFormatError("Generated device pad name already exists.")
            self.store.write_asset(device.footprint_path, pending.content)
        except (KiCadFormatError, ProjectFormatError) as error:
            QMessageBox.warning(self, "Cannot place device", str(error))
            return
        self.project.devices.append(device)
        self.project.pads.extend(generated)
        self._device_footprint_cache[device.footprint_path] = pending.footprint
        self._pending_device = None
        self._clear_device_previews()
        self._dirty = True
        self.statusBar().clearMessage()
        LOGGER.info(
            "Device placed id=%s reference=%s side=%s rotation=%s pads=%s",
            device.device_id,
            device.reference,
            side,
            device.rotation,
            len(generated),
        )
        self._refresh_views()
        self._apply_active_view_state(view_state)
        self._update_title()

    def _clear_device_previews(self) -> None:
        for view in (*self._views.values(), *self._side_views.values()):
            view.clear_device_placement()

    def _cancel_device_placement(self, clear_status: bool = True) -> None:
        """Cancel a pending footprint without changing project data."""
        self._pending_device = None
        self._clear_device_previews()
        if clear_status:
            self.statusBar().clearMessage()

    def create_pad(self) -> None:
        """Start rectangle placement on the currently visible board view."""
        if not self.project:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        self._cancel_device_placement()
        name = self._next_pad_name()
        LOGGER.info(
            "Create pad requested name=%s tab=%s", name, self._tabs.currentIndex()
        )
        if self._tabs.currentIndex() == 0:
            views = {"top": self._views["top"]}
        elif self._tabs.currentIndex() == 1:
            views = {"bottom": self._views["bottom"]}
        elif self._tabs.currentIndex() == 2:
            views = self._side_views
        else:
            self._tabs.setCurrentIndex(2)
            views = self._side_views
        if not any(view.has_image() for view in views.values()):
            QMessageBox.information(self, "No image", "Import an image first.")
            return
        for candidate in (*self._views.values(), *self._side_views.values()):
            candidate.set_pad_placement(False)
        self._pending_pad = Pad(name, "top", 0.0, 0.0)
        for view in views.values():
            if view.has_image():
                view.set_pad_placement(True)
        LOGGER.debug("Pad placement armed name=%s views=%s", name, tuple(views))
        self.statusBar().showMessage(f"Draw a rectangle for pad {name}.")

    def _show_log_path(self) -> None:
        """Display the diagnostic log path for bug reports."""
        path = _LOG_PATH or _configure_logging()
        QMessageBox.information(self, "Log file", str(path))

    def _next_pad_name(self) -> str:
        """Return the first unused automatic pad name."""
        names = {pad.name for pad in self.project.pads} if self.project else set()
        index = 1
        while f"P{index}" in names:
            index += 1
        return f"P{index}"

    def _place_pad(
        self, side: str, x: float, y: float, width: float, height: float
    ) -> None:
        """Finish pad placement at normalized image coordinates."""
        pending = self._pending_pad
        if not self.project or pending is None:
            LOGGER.warning("Ignored pad placement side=%s without pending pad", side)
            return
        for view in (*self._views.values(), *self._side_views.values()):
            view.set_pad_placement(False)
        self.project.pads.append(
            Pad(pending.name, side, x, y, pending.pad_id, width, height)
        )
        LOGGER.info(
            "Pad created id=%s name=%s side=%s rect=(%.5f,%.5f,%.5f,%.5f)",
            pending.pad_id,
            pending.name,
            side,
            x,
            y,
            width,
            height,
        )
        self._pending_pad = None
        self._dirty = True
        self.statusBar().clearMessage()
        self._schedule_pad_refresh(self._active_views()[0].view_state())
        self._update_title()

    def _pad_at(self, side: str, x: float, y: float) -> Pad | None:
        """Return the pad containing normalized coordinates, if any."""
        if not self.project:
            return None
        pixmap = self._base_pixmap_for_asset(side)
        image_width = pixmap.width() if not pixmap.isNull() else 1
        image_height = pixmap.height() if not pixmap.isNull() else 1
        for pad in reversed(self.project.pads):
            if pad.side == side and self._pad_contains(
                pad, x, y, image_width, image_height
            ):
                return pad
        return None

    @staticmethod
    def _pad_contains(
        pad: Pad, x: float, y: float, image_width: int, image_height: int
    ) -> bool:
        """Hit-test a potentially rotated pad in normalized coordinates."""
        center_x = pad.x + pad.width / 2
        center_y = pad.y + pad.height / 2
        cosine = math.cos(math.radians(-pad.rotation))
        sine = math.sin(math.radians(-pad.rotation))
        delta_x = (x - center_x) * image_width
        delta_y = (y - center_y) * image_height
        local_x = delta_x * cosine - delta_y * sine
        local_y = delta_x * sine + delta_y * cosine
        width = pad.width * image_width
        height = pad.height * image_height
        if pad.shape in {"circle", "oval"}:
            return (local_x / (width / 2)) ** 2 + (local_y / (height / 2)) ** 2 <= 1.0
        return abs(local_x) <= width / 2 and abs(local_y) <= height / 2

    def _select_pad(self, side: str, x: float, y: float) -> None:
        """Toggle same-net connections without changing the current view."""
        view_state = self._active_views()[0].view_state()
        pad = self._pad_at(side, x, y)
        if pad and pad.pad_id == self._selected_pad_id:
            self._selected_net = None
            self._selected_pad_id = None
        else:
            self._selected_net = pad.net if pad else None
            self._selected_pad_id = pad.pad_id if pad else None
        LOGGER.info(
            "Pad click side=%s point=(%.5f,%.5f) pad=%s net=%s selected=%s",
            side,
            x,
            y,
            pad.pad_id if pad else None,
            pad.net if pad else None,
            self._selected_pad_id,
        )
        self._schedule_pad_refresh(view_state)

    def _set_pads_visible(self, visible: bool) -> None:
        """Show or hide pad overlays while preserving the current view."""
        self._pads_visible = visible
        LOGGER.info("Pad visibility=%s", visible)
        self._schedule_pad_refresh(self._active_views()[0].view_state())

    def _schedule_pad_refresh(self, state: tuple[float, float, float]) -> None:
        """Refresh pad overlays after the current mouse event has completed."""
        self._pending_pad_view_state = state
        if self._pad_refresh_pending:
            return
        self._pad_refresh_pending = True
        LOGGER.debug("Pad refresh scheduled state=%s", state)
        QTimer.singleShot(0, self._finish_pad_refresh)

    def _finish_pad_refresh(self) -> None:
        """Run one coalesced pad refresh outside mouse event handlers."""
        state = self._pending_pad_view_state
        self._pending_pad_view_state = None
        self._pad_refresh_pending = False
        LOGGER.debug("Pad refresh started state=%s", state)
        self._refresh_views()
        if state is not None:
            self._apply_active_view_state(state)

    def _apply_active_view_state(self, state: tuple[float, float, float]) -> None:
        """Restore zoom and pan after refreshing visible pad markers."""
        self._syncing_views = True
        try:
            for view in self._active_views():
                view.apply_view_state(state)
        finally:
            self._syncing_views = False
        if self._tabs.currentIndex() not in (3, 4):
            self._sync_board_views(self._active_views()[0])

    def _connect_pad_to_net(self, side: str, x: float, y: float) -> None:
        """Open non-blocking net assignment for a right-clicked pad."""
        if not self.project:
            return
        pad = self._pad_at(side, x, y)
        if pad is None:
            return
        if self._net_dialog is not None:
            self._net_dialog.close()
        dialog = QInputDialog(self)
        dialog.setWindowTitle("Connect pad to net")
        dialog.setLabelText(f"Net for {pad.name}:")
        dialog.setTextValue(pad.net or "")
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.accepted.connect(
            lambda pad_id=pad.pad_id, dialog=dialog: self._assign_pad_net(
                pad_id, dialog.textValue()
            )
        )
        dialog.finished.connect(
            lambda result, dialog=dialog: self._net_dialog_finished(dialog, result)
        )
        self._net_dialog = dialog
        LOGGER.info("Net dialog opened id=%s pad=%s", pad.pad_id, pad.name)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _assign_pad_net(self, pad_id: str, value: str) -> None:
        """Persist a net value submitted by the asynchronous dialog."""
        if not self.project:
            return
        pad = next((item for item in self.project.pads if item.pad_id == pad_id), None)
        if pad is None:
            return
        net = value.strip() or None
        self.project.pads = [
            replace(item, net=net) if item.pad_id == pad_id else item
            for item in self.project.pads
        ]
        self._selected_net = net
        self._selected_pad_id = pad_id
        LOGGER.info("Pad net assigned id=%s pad=%s net=%s", pad_id, pad.name, net)
        self._dirty = True
        self._schedule_pad_refresh(self._active_views()[0].view_state())
        self._update_title()

    def _net_dialog_finished(self, dialog: QInputDialog, _result: int) -> None:
        """Release the asynchronous net dialog reference."""
        LOGGER.debug("Net dialog closed")
        if self._net_dialog is dialog:
            self._net_dialog = None

    def _defer_pad_net_assignment(self, side: str, x: float, y: float) -> None:
        """Open net assignment after returning from the mouse event."""
        QTimer.singleShot(0, lambda: self._connect_pad_to_net(side, x, y))

    def _defer_pad_menu(self, side: str, x: float, y: float) -> None:
        """Open the pad action menu after returning from the mouse event."""
        QTimer.singleShot(0, lambda: self._show_pad_menu(side, x, y))

    def _show_pad_menu(self, side: str, x: float, y: float) -> None:
        """Show non-blocking actions for the pad under Shift+click."""
        pad = self._pad_at(side, x, y)
        if pad is None:
            return
        if self._pad_menu is not None:
            self._pad_menu.close()
        menu = QMenu(self)
        delete_action = menu.addAction("Delete pad")
        connect_action = menu.addAction("Connect to net")
        disconnect_action = menu.addAction("Disconnect")
        disconnect_action.setEnabled(pad.net is not None)
        delete_action.triggered.connect(lambda: self._delete_pad(pad.pad_id))
        connect_action.triggered.connect(lambda: self._connect_pad_to_net(side, x, y))
        disconnect_action.triggered.connect(
            lambda: self._assign_pad_net(pad.pad_id, "")
        )
        menu.aboutToHide.connect(lambda menu=menu: self._pad_menu_closed(menu))
        self._pad_menu = menu
        LOGGER.info("Pad menu opened id=%s pad=%s", pad.pad_id, pad.name)
        menu.popup(QCursor.pos())

    def _pad_menu_closed(self, menu: QMenu) -> None:
        """Release a closed asynchronous pad menu."""
        if self._pad_menu is menu:
            self._pad_menu = None

    def _delete_pad(self, pad_id: str) -> None:
        """Delete one pad selected from the Shift+click menu."""
        if not self.project:
            return
        pad = next((item for item in self.project.pads if item.pad_id == pad_id), None)
        if pad is None:
            return
        self.project.pads = [
            item for item in self.project.pads if item.pad_id != pad_id
        ]
        if self._selected_pad_id == pad_id:
            self._selected_pad_id = None
            self._selected_net = None
        self._dirty = True
        LOGGER.info("Pad deleted id=%s pad=%s", pad.pad_id, pad.name)
        self._schedule_pad_refresh(self._active_views()[0].view_state())
        self._update_title()

    def _log_ui_heartbeat(self) -> None:
        """Record that the Qt event loop is still processing events."""
        LOGGER.debug(
            "UI heartbeat pending_pad=%s refresh_pending=%s net_dialog=%s",
            self._pending_pad.name if self._pending_pad else None,
            self._pad_refresh_pending,
            self._net_dialog is not None,
        )

    def _create_actions(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        for label, handler, shortcut in (
            ("New project", self.new_project, "Ctrl+N"),
            ("Open project", self.open_project, "Ctrl+O"),
            ("Save project", self.save_project, "Ctrl+S"),
            ("Close project", self.close_project, "Ctrl+W"),
        ):
            action = QAction(label, self)
            action.triggered.connect(handler)
            action.setShortcut(shortcut)
            file_menu.addAction(action)
        self._import_action = QAction("Import image", self)
        self._import_action.setShortcut("I")
        self._import_action.setToolTip("Import image, then choose Top or Bottom")
        self._import_action.triggered.connect(self.import_picture)
        file_menu.addAction(self._import_action)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """Switch board view with `T`, `B`, or a double press."""
        key = event.key()
        if key == Qt.Key.Key_Escape and self._pending_device is not None:
            self._cancel_device_placement()
            event.accept()
            return
        if key not in (Qt.Key.Key_T, Qt.Key.Key_B):
            super().keyPressEvent(event)
            return
        now = monotonic()
        is_double = key == self._last_view_key and now - self._last_view_time < 0.4
        self._tabs.setCurrentIndex(
            2 if is_double else (0 if key == Qt.Key.Key_T else 1)
        )
        self._last_view_key = None if is_double else key
        self._last_view_time = now
        event.accept()

    def new_project(self) -> None:
        """Create an empty `.revp` project file."""
        if not self._confirm_pending_changes():
            return
        dialog = ProjectDetailsDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Create project", "", "Tnasrevner project (*.revp)"
        )
        if not path:
            return
        project_path = Path(path)
        if project_path.suffix.lower() != ".revp":
            project_path = project_path.with_suffix(".revp")
        self.store = ProjectStore(project_path)
        self.project = ProjectDocument(
            dialog.project_name.text(), dialog.board_name.text()
        )
        self._image_cache.clear()
        self._device_footprint_cache.clear()
        self._cancel_device_placement()
        self._dirty = True
        self._refresh_views()
        self._update_title()

    def open_project(self) -> None:
        """Open a `.revp` project file."""
        if not self._confirm_pending_changes():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", "", "Tnasrevner project (*.revp)"
        )
        if not path:
            return
        try:
            store = ProjectStore(Path(path))
            project = store.load()
        except ProjectFormatError as error:
            QMessageBox.critical(self, "Open failed", str(error))
            return
        self.store, self.project, self._dirty = store, project, False
        self._image_cache.clear()
        self._device_footprint_cache.clear()
        self._cancel_device_placement()
        self._refresh_views()
        self._update_title()

    def save_project(self) -> bool:
        """Save project metadata and current display tab."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return False
        self.project.display.mode = (
            "top",
            "bottom",
            "side_by_side",
            "both",
            "nets",
        )[self._tabs.currentIndex()]
        display_view = self._active_views()[0]
        (
            self.project.display.zoom,
            self.project.display.pan_x,
            self.project.display.pan_y,
        ) = display_view.view_state()
        try:
            self.store.save(self.project)
        except (OSError, ProjectFormatError) as error:
            QMessageBox.critical(self, "Save failed", str(error))
            return False
        self._dirty = False
        self._update_title()
        return True

    def close_project(self) -> None:
        """Close current project after resolving pending changes."""
        if not self._confirm_pending_changes():
            return
        self.project = None
        self.store = None
        self._image_cache.clear()
        self._device_footprint_cache.clear()
        self._cancel_device_placement()
        self._dirty = False
        self._refresh_views()
        self._update_title()

    def _confirm_pending_changes(self) -> bool:
        """Ask how to handle unsaved changes before changing project context."""
        if not self._dirty:
            return True
        answer = QMessageBox.warning(
            self,
            "Unsaved changes",
            "Save changes before continuing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save_project()
        return answer == QMessageBox.StandardButton.Discard

    def closeEvent(
        self, event: QCloseEvent
    ) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Prevent accidental loss of unsaved project changes."""
        if self._close_when_cache_finishes:
            event.accept()
            return
        if not self._confirm_pending_changes():
            event.ignore()
            return
        if (
            self._kicad_cache_thread is not None
            and self._kicad_cache_thread.isRunning()
        ):
            self._pending_device_reference = None
            self._close_kicad_progress()
            self._close_when_cache_finishes = True
            self.hide()
            event.ignore()
            return
        event.accept()

    def import_picture(self) -> None:
        """Import an image, then ask whether it belongs to top or bottom."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        source, _ = QFileDialog.getOpenFileName(
            self,
            "Import image",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)",
        )
        if not source:
            return
        source_path = Path(source)
        if QPixmap(str(source_path)).isNull():
            QMessageBox.warning(
                self, "Import failed", "Selected file is not a readable image."
            )
            return
        side = self._choose_image_side()
        if side is None:
            return
        edited = self._edit_imported_image(QPixmap(str(source_path)))
        if edited is None:
            return
        edited_image, pixels_per_mm, calibration_line = edited
        relative_path = f"assets/{side}.png"
        original_path = f"assets/original/{side}{source_path.suffix.lower()}"
        try:
            self.store.write_asset(original_path, source_path.read_bytes())
        except OSError as error:
            QMessageBox.critical(self, "Import failed", str(error))
            return
        self.store.write_asset(relative_path, self._pixmap_bytes(edited_image))
        self.project.images = [
            image for image in self.project.images if image.side != side
        ]
        self.project.images.append(
            ImageAsset(
                side,
                relative_path,
                source_path.name,
                pixels_per_mm,
                original_path,
                calibration_line,
            )
        )
        self._image_cache.pop(side, None)
        self._dirty = True
        self._refresh_views()
        self._update_title()

    def remove_picture(self) -> None:
        """Remove selected Top or Bottom image after explicit side choice."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        side = self._choose_image_side()
        if side is None:
            return
        asset = next(
            (image for image in self.project.images if image.side == side), None
        )
        if asset is None:
            QMessageBox.information(self, "No image", f"No {side} image imported.")
            return
        self.store.remove_asset(asset.path)
        if asset.original_path:
            self.store.remove_asset(asset.original_path)
        self.project.images = [
            image for image in self.project.images if image.side != side
        ]
        self._image_cache.pop(side, None)
        self._dirty = True
        self._refresh_views()
        self._update_title()

    def edit_picture(self) -> None:
        """Reopen the working image for crop and rotation adjustments."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        side = self._choose_image_side()
        if side is None:
            return
        asset = next(
            (image for image in self.project.images if image.side == side), None
        )
        if asset is None:
            QMessageBox.information(self, "No image", f"No {side} image imported.")
            return
        image = QPixmap()
        try:
            image.loadFromData(self.store.read_asset(asset.path))
        except ProjectFormatError as error:
            QMessageBox.warning(self, "Edit failed", str(error))
            return
        if image.isNull():
            QMessageBox.warning(self, "Edit failed", "Working image is unreadable.")
            return
        edited = self._edit_imported_image(
            image, asset.pixels_per_mm, asset.calibration_line
        )
        if edited is None:
            return
        edited_image, pixels_per_mm, calibration_line = edited
        self.store.write_asset(asset.path, self._pixmap_bytes(edited_image))
        self.project.images = [
            (
                ImageAsset(
                    image.side,
                    image.path,
                    image.original_name,
                    pixels_per_mm,
                    image.original_path,
                    calibration_line,
                )
                if image.side == side
                else image
            )
            for image in self.project.images
        ]
        self._image_cache.pop(side, None)
        self._dirty = True
        self._refresh_views()
        self._update_title()

    def _edit_imported_image(
        self,
        image: QPixmap,
        pixels_per_mm: float | None = None,
        calibration_line: tuple[float, float, float, float] | None = None,
    ) -> tuple[QPixmap, float, tuple[float, float, float, float] | None] | None:
        """Open editor and return image plus scale, or `None` on cancel."""
        dialog = ImageEditDialog(image, self)
        if pixels_per_mm is not None:
            dialog.prepare_existing_image(pixels_per_mm, calibration_line)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return (
            dialog.result_pixmap(),
            dialog.pixels_per_mm(),
            dialog.calibration_line(),
        )

    @staticmethod
    def _pixmap_bytes(image: QPixmap) -> bytes:
        """Encode edited image as PNG bytes for archive storage."""
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.ReadWrite)
        if not image.save(buffer, "PNG"):
            raise ProjectFormatError("cannot encode imported image")
        return bytes(buffer.data())

    def _choose_image_side(self) -> str | None:
        """Ask which external board side the selected image represents."""
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Image side")
        dialog.setText("Where does this image belong?")
        top_button = dialog.addButton("Top", QMessageBox.ButtonRole.AcceptRole)
        bottom_button = dialog.addButton("Bottom", QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        dialog.exec()
        if dialog.clickedButton() is top_button:
            return "top"
        if dialog.clickedButton() is bottom_button:
            return "bottom"
        return None

    def _refresh_views(self) -> None:
        """Refresh all views from one composed pixmap per board side."""
        images = {side: self._pixmap_for_asset(side) for side in ("top", "bottom")}
        for side, view in self._views.items():
            view.set_pixmap(images[side])
        self._refresh_overlay(images)
        for side, view in self._side_views.items():
            view.set_pixmap(images[side])
        self._refresh_net_table()
        if self.project:
            self._tabs.setCurrentIndex(
                {
                    "top": 0,
                    "bottom": 1,
                    "side_by_side": 2,
                    "both": 3,
                    "nets": 4,
                }[self.project.display.mode]
            )
            state = (
                self.project.display.zoom,
                self.project.display.pan_x,
                self.project.display.pan_y,
            )
            self._syncing_views = True
            try:
                for view in self._active_views():
                    view.apply_view_state(state)
            finally:
                self._syncing_views = False
            if self._tabs.currentIndex() not in (3, 4):
                self._sync_board_views(self._active_views()[0])
        else:
            self._sync_board_views(self._views["top"])
        LOGGER.debug("View refresh completed")

    def _refresh_net_table(self) -> None:
        """Show each stable pad name and its assigned electrical net."""
        pads = (
            sorted(self.project.pads, key=lambda pad: pad.name) if self.project else []
        )
        self._net_table.setRowCount(len(pads))
        for row, pad in enumerate(pads):
            self._net_table.setItem(row, 0, QTableWidgetItem(pad.name))
            self._net_table.setItem(row, 1, QTableWidgetItem(pad.net or ""))

    def _refresh_overlay(self, images: dict[str, QPixmap]) -> None:
        """Compose top and mirrored bottom images into the Both view."""
        top = images["top"]
        bottom = images["bottom"]
        base = top if not top.isNull() else bottom
        if base.isNull():
            self._overlay_view.set_pixmap(QPixmap())
            return
        canvas = QPixmap(base.size())
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        if not bottom.isNull():
            mirrored = bottom.scaled(
                base.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ).transformed(QTransform().scale(-1, 1))
            painter.setOpacity(0.5)
            painter.drawPixmap(0, 0, mirrored)
        if not top.isNull():
            top = top.scaled(
                base.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.setOpacity(0.5 if not bottom.isNull() else 1.0)
            painter.drawPixmap(0, 0, top)
        painter.end()
        self._overlay_view.set_pixmap(canvas)

    def _pixmap_for_asset(self, side: str) -> QPixmap:
        """Copy one cached working image and draw device/pad overlays."""
        pixmap = self._base_pixmap_for_asset(side)
        if pixmap.isNull():
            return pixmap
        pixmap = QPixmap(pixmap)
        self._draw_devices(pixmap, side)
        self._draw_pads(pixmap, side)
        return pixmap

    def _draw_devices(self, pixmap: QPixmap, side: str) -> None:
        """Draw every persisted KiCad footprint at calibrated physical scale."""
        if not self.project or not self.store:
            return
        image = next(
            (asset for asset in self.project.images if asset.side == side), None
        )
        if image is None or image.pixels_per_mm is None:
            return
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for device in self.project.devices:
            if device.side != side:
                continue
            footprint = self._footprint_for_device(device)
            if footprint is None:
                continue
            painter.save()
            painter.translate(
                device.x * (pixmap.width() - 1),
                device.y * (pixmap.height() - 1),
            )
            _paint_footprint(
                painter,
                footprint,
                image.pixels_per_mm,
                side,
                device.rotation,
            )
            painter.restore()
        painter.end()

    def _footprint_for_device(self, device: Device) -> Footprint | None:
        """Decode and cache an embedded footprint used by a placed device."""
        cached = self._device_footprint_cache.get(device.footprint_path)
        if cached is not None:
            return cached
        if self.store is None:
            return None
        try:
            footprint = parse_footprint(
                self.store.read_asset(device.footprint_path),
                device.footprint_library,
            )
        except (ProjectFormatError, KiCadFormatError) as error:
            LOGGER.warning(
                "Cannot render device footprint path=%s error=%s",
                device.footprint_path,
                error,
            )
            return None
        self._device_footprint_cache[device.footprint_path] = footprint
        return footprint

    def _base_pixmap_for_asset(self, side: str) -> QPixmap:
        """Load and cache one undecorated working image."""
        if not self.project or not self.store:
            return QPixmap()
        asset = next(
            (image for image in self.project.images if image.side == side), None
        )
        if not asset:
            return QPixmap()
        if side in self._image_cache:
            return self._image_cache[side]
        try:
            if self.store.is_archive:
                pixmap = QPixmap()
                pixmap.loadFromData(self.store.read_asset(asset.path))
            else:
                pixmap = QPixmap(str(self.store.root / asset.path))
        except ProjectFormatError as error:
            QMessageBox.warning(self, "Image unavailable", str(error))
            return QPixmap()
        if pixmap.isNull():
            QMessageBox.warning(
                self, "Image unavailable", f"Cannot decode image asset: {asset.path}"
            )
            return pixmap
        self._image_cache[side] = pixmap
        LOGGER.debug(
            "Cached working image side=%s size=%sx%s",
            side,
            pixmap.width(),
            pixmap.height(),
        )
        return pixmap

    def _draw_pads(  # pylint: disable=too-many-locals
        self, pixmap: QPixmap, side: str
    ) -> None:
        """Draw persisted pad markers over one board-side image."""
        if not self._pads_visible or not self.project or not self.project.pads:
            return
        painter = QPainter(pixmap)
        radius = max(5, min(pixmap.width(), pixmap.height()) // 100)
        pads = [pad for pad in self.project.pads if pad.side == side]
        if self._selected_net:
            centers = {
                pad.pad_id: QPoint(
                    round((pad.x + pad.width / 2) * (pixmap.width() - 1)),
                    round((pad.y + pad.height / 2) * (pixmap.height() - 1)),
                )
                for pad in pads
                if pad.net == self._selected_net
            }
            origin_id = (
                self._selected_pad_id if self._selected_pad_id in centers else None
            )
            if origin_id is None and centers:
                origin_id = next(iter(centers))
            origin = centers.get(origin_id or "")
            painter.setPen(QPen(Qt.GlobalColor.white, max(2, radius // 2)))
            if origin is not None:
                for pad_id, target in centers.items():
                    if pad_id != origin_id:
                        painter.drawLine(origin, target)
        painter.setOpacity(0.45)
        painter.setBrush(Qt.GlobalColor.red)
        for pad in pads:
            painter.setPen(
                QPen(
                    (
                        Qt.GlobalColor.white
                        if pad.pad_id == self._selected_pad_id
                        else Qt.GlobalColor.yellow
                    ),
                    (
                        max(2, radius // 2)
                        if pad.pad_id == self._selected_pad_id
                        else max(2, radius // 3)
                    ),
                )
            )
            left = round(pad.x * (pixmap.width() - 1))
            top = round(pad.y * (pixmap.height() - 1))
            right = max(left + 2, round((pad.x + pad.width) * (pixmap.width() - 1)))
            bottom = max(top + 2, round((pad.y + pad.height) * (pixmap.height() - 1)))
            width, height = right - left, bottom - top
            center = QPointF(left + width / 2, top + height / 2)
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
            painter.setOpacity(1.0)
            painter.drawText(
                QRect(left, top, width, height),
                Qt.AlignmentFlag.AlignCenter,
                pad.name,
            )
            painter.setOpacity(0.45)
        painter.end()

    def _update_title(self) -> None:
        name = self.project.project_name if self.project else "No project"
        self.setWindowTitle(f"Tnasrevner — {name}{' *' if self._dirty else ''}")


def main() -> int:
    """Run Tnasrevner GUI."""
    app = QApplication(sys.argv)
    app.setApplicationName("Tnasrevner")
    app.setApplicationDisplayName("Tnasrevner")
    _configure_logging()
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
