"""Basic desktop GUI for creating projects and displaying board pictures."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,invalid-name
# pylint: disable=too-many-lines
# pylint: disable=too-many-locals,too-many-statements
# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches

from collections.abc import Callable
from dataclasses import dataclass, replace
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
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QComboBox,
    QFileDialog,
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
    ComponentPin,
    Device,
    ImageAsset,
    Pad,
    ProjectDocument,
    ProjectFormatError,
    ProjectStore,
)


def _application_icon() -> QIcon:
    """Load application icon from package data."""
    return QIcon(str(Path(__file__).with_name("assets") / "tnasrevner.svg"))


def _center_tool_icon() -> QIcon:
    """Create a clear crosshair icon for image centering."""
    canvas = QPixmap(24, 24)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(80, 160, 220), 2))
    painter.drawEllipse(QRectF(6, 6, 12, 12))
    painter.drawLine(QPointF(12, 1), QPointF(12, 6))
    painter.drawLine(QPointF(12, 18), QPointF(12, 23))
    painter.drawLine(QPointF(1, 12), QPointF(6, 12))
    painter.drawLine(QPointF(18, 12), QPointF(23, 12))
    painter.end()
    return QIcon(canvas)


def _pad_tool_icon() -> QIcon:
    """Create a yellow rectangle icon for adding a pad."""
    canvas = QPixmap(24, 24)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(150, 110, 0), 2))
    painter.setBrush(QColor(255, 220, 45))
    painter.drawRoundedRect(QRectF(4, 7, 16, 10), 2, 2)
    painter.end()
    return QIcon(canvas)


def _save_tool_icon() -> QIcon:
    """Create a recognizable floppy-disk save icon."""
    canvas = QPixmap(24, 24)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(35, 65, 100), 2))
    painter.setBrush(QColor(70, 135, 205))
    painter.drawRoundedRect(QRectF(4, 3, 16, 18), 2, 2)
    painter.setPen(QPen(QColor(225, 240, 255), 1.5))
    painter.setBrush(QColor(225, 240, 255))
    painter.drawRect(QRectF(7, 4, 8, 6))
    painter.drawRoundedRect(QRectF(7, 14, 10, 5), 1, 1)
    painter.end()
    return QIcon(canvas)


LOGGER = logging.getLogger("tnasrevner")
_LOG_PATH: Path | None = None
_DEVICE_REFERENCE = re.compile(r"[A-Za-z][A-Za-z0-9_.+\-]{0,63}\Z")
_NUMBERED_DEVICE_REFERENCE = re.compile(r"([A-Za-z]+)([0-9]+)\Z")
_RECENT_FOOTPRINTS_KEY = "kicad/recent-footprints"
_REFERENCE_PREFIX_KEY = "kicad/reference-prefix"
_LAST_PROJECT_DIRECTORY_KEY = "projects/last-directory"
_MAX_RECENT_FOOTPRINTS = 5
_DEFAULT_REFERENCE_PREFIXES = {
    "antenna": "AE",
    "battery": "BT",
    "capacitor": "C",
    "connector": "J",
    "crystal": "Y",
    "diode": "D",
    "fuse": "F",
    "inductor": "L",
    "jumper": "JP",
    "led": "D",
    "mountinghole": "H",
    "oscillator": "Y",
    "potentiometer": "RV",
    "relay": "K",
    "resistor": "R",
    "switch": "SW",
    "testpoint": "TP",
    "thermistor": "RT",
    "transformer": "T",
    "transistor": "Q",
    "varistor": "RV",
}


def _footprint_family(library: str) -> str:
    """Return a readable component family from a KiCad library name."""
    return library.split("_", maxsplit=1)[0].replace("-", " ")


def _footprint_family_key(library: str) -> str:
    """Return the stable settings key used for one component family."""
    return _footprint_family(library).casefold().replace(" ", "-")


def _reference_sort_key(reference: str) -> tuple[str, int, str]:
    """Sort common references naturally, so C2 appears before C10."""
    match = _NUMBERED_DEVICE_REFERENCE.fullmatch(reference)
    if match:
        return match.group(1).casefold(), int(match.group(2)), ""
    return reference.casefold(), -1, reference.casefold()


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


class MeasurementSpinBox(QDoubleSpinBox):
    """Decimal measurement input accepting both `.` and `,` separators."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setLocale(QLocale.c())

    def validate(self, text: str, position: int):
        """Validate comma input with the locale-independent decimal parser."""
        state, _normalized, _position = super().validate(
            text.replace(",", "."), position
        )
        return state, text, position

    def valueFromText(self, text: str) -> float:  # noqa: N802
        """Parse either common decimal separator without changing magnitude."""
        return super().valueFromText(text.replace(",", "."))


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

    def __init__(
        self,
        image: QPixmap,
        parent: QWidget | None = None,
        footprint_selector: Callable[[QWidget], Footprint | None] | None = None,
        load_callback: Callable[[], None] | None = None,
        remove_callback: Callable[[], None] | None = None,
    ) -> None:  # pylint: disable=too-many-locals,too-many-statements
        super().__init__(parent)
        self.setWindowTitle("Edit imported image")
        self.resize(1000, 700)
        self._base_image = image
        self._source = image
        self._angle = 0.0
        self._zoom = 1.0
        self._zoom_revision = 0
        self._display_scale = 1.0
        self._selection_start: QPoint | None = None
        self._selection = None
        self._editing_existing_image = False
        self._crop_selection_modified = False
        self._resize_edges: set[str] = set()
        self._calibration_start: QPoint | None = None
        self._calibration_end: QPoint | None = None
        self._calibration_line: tuple[QPointF, QPointF] | None = None
        self._result_calibration_line: tuple[float, float, float, float] | None = None
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
        calibration_button = QPushButton("Scale line")
        calibration_button.setCheckable(True)
        calibration_button.setChecked(True)
        calibration_button.clicked.connect(lambda: self._set_edit_mode("calibration"))
        footprint_button = QPushButton("Scale footprint")
        footprint_button.setToolTip("Choose a KiCad footprint as a physical reference")
        footprint_button.setEnabled(footprint_selector is not None)
        footprint_button.clicked.connect(self._choose_footprint_calibration)
        crop_button = QPushButton("Crop rectangle")
        crop_button.setCheckable(True)
        crop_button.clicked.connect(lambda: self._set_edit_mode("crop"))
        self._calibration_button = calibration_button
        self._footprint_button = footprint_button
        self._crop_button = crop_button
        load_image_button = QPushButton("Load")
        load_image_button.setToolTip("Load another image for this side")
        load_image_button.setEnabled(load_callback is not None)
        if load_callback is not None:
            load_image_button.clicked.connect(
                lambda: self._close_for_image_action(load_callback)
            )
        remove_image_button = QPushButton("Remove")
        remove_image_button.setToolTip("Remove image for this side")
        remove_image_button.setEnabled(remove_callback is not None)
        if remove_callback is not None:
            remove_image_button.clicked.connect(
                lambda: self._close_for_image_action(remove_callback)
            )
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
        controls = QHBoxLayout()
        controls.addWidget(calibration_button)
        controls.addWidget(footprint_button)
        controls.addWidget(crop_button)
        controls.addWidget(load_image_button)
        controls.addWidget(remove_image_button)
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
        layout.addWidget(self._scroll)
        layout.addLayout(controls)
        self.showMaximized()
        self._render()
        QTimer.singleShot(0, self._render)

    def _close_for_image_action(self, callback: Callable[[], None]) -> None:
        """Close editor before changing its project-side image."""
        self.reject()
        QTimer.singleShot(0, callback)

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
            self._crop_selection_modified = True
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

    def _set_edit_mode(self, mode: str) -> None:
        """Switch between scale-line and crop-rectangle drawing."""
        self._edit_mode = mode
        if mode == "calibration":
            self._calibration_method = "line"
            self._millimeters.setEnabled(True)
        elif mode == "footprint":
            self._calibration_method = "footprint"
            self._millimeters.setEnabled(False)
        self._calibration_button.setChecked(mode == "calibration")
        self._footprint_button.setChecked(mode == "footprint")
        self._crop_button.setChecked(mode == "crop")

    def _choose_footprint_calibration(self) -> None:
        """Select and place a KiCad footprint for physical calibration."""
        if self._footprint_selector is None:
            return
        footprint = self._footprint_selector(self)
        if footprint is None:
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
        """Rotate source image and clear old selection."""
        self._set_angle(self._angle + angle)

    def _set_angle(self, angle: float) -> None:
        """Apply free rotation relative to original imported image."""
        self._zoom_revision += 1
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
        footprint_ratio = None
        if self._footprint_center is not None:
            footprint_ratio = (
                self._footprint_center.x() / old_size.width(),
                self._footprint_center.y() / old_size.height(),
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
        if footprint_ratio is not None:
            self._footprint_center = QPointF(
                footprint_ratio[0] * new_size.width(),
                footprint_ratio[1] * new_size.height(),
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
        centered = anchor is None
        self._zoom_revision += 1
        revision = self._zoom_revision
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
        self._zoom = max(0.1, min(self._zoom * factor, 20.0))
        self._render()
        self._restore_selection(selection_ratio)
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
        self._render()
        self._restore_selection(selection_ratio)

    def _render(self) -> None:
        """Fit source image to editor viewport."""
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
        self._rubber_band.setGeometry(self._canvas.rect())
        self._rubber_band.set_selection(self._selection)
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


class FootprintPickerDialog(QDialog):  # pylint: disable=R0902,R0903
    """Search and select one cached KiCad footprint."""

    _MAX_RESULTS = 750

    def __init__(
        self,
        references: tuple[FootprintReference, ...],
        parent: QWidget | None = None,
        recent_identifiers: tuple[str, ...] = (),
        pad_counts: dict[str, int] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select KiCad footprint")
        self.resize(980, 560)
        self._references = references
        self._recent_identifiers = recent_identifiers[:_MAX_RECENT_FOOTPRINTS]
        self._pad_counts = pad_counts or {}
        self._visible_references: list[FootprintReference] = []
        self._preview_cache: dict[str, Footprint] = {}
        self._pad_count = QSpinBox(self)
        self._pad_count.setRange(0, 9999)
        self._pad_count.setValue(0)
        self._pad_count.setPrefix("Pads: ")
        self._pad_count.setToolTip("0 shows footprints with any number of pads")
        minus_button = QPushButton("−", self)
        minus_button.setToolTip("Show one fewer pad")
        plus_button = QPushButton("+", self)
        plus_button.setToolTip("Show one more pad")
        self._search = QLineEdit(self)
        self._search.setPlaceholderText("Search library or footprint name…")
        self._list = QListWidget(self)
        self._status = QLabel(self)
        self._preview = QLabel("Select a footprint", self)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setMinimumSize(360, 360)
        self._preview.setStyleSheet("background: #202124; color: #d0d0d0;")
        self._preview_size = QLabel("Size: —", self)
        self._preview_size.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        layout = QVBoxLayout(self)
        content = QHBoxLayout()
        picker = QVBoxLayout()
        picker.addWidget(self._search)
        picker.addWidget(self._list)
        picker.addWidget(self._status)
        content.addLayout(picker, 3)
        preview_layout = QVBoxLayout()
        preview_layout.addWidget(self._preview)
        preview_layout.addWidget(self._preview_size)
        content.addLayout(preview_layout, 2)
        layout.addLayout(content)
        pad_filter = QHBoxLayout()
        pad_filter.addWidget(QLabel("Filter by pad count"))
        pad_filter.addStretch()
        pad_filter.addWidget(minus_button)
        pad_filter.addWidget(self._pad_count)
        pad_filter.addWidget(plus_button)
        layout.addLayout(pad_filter)
        layout.addWidget(self._buttons)
        self._search.textChanged.connect(self._refresh_results)
        self._pad_count.valueChanged.connect(lambda _value: self._refresh_results())
        minus_button.clicked.connect(self._decrease_pad_filter)
        plus_button.clicked.connect(self._increase_pad_filter)
        self._list.currentRowChanged.connect(self._selection_changed)
        self._list.itemDoubleClicked.connect(lambda _item: self.accept())
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._refresh_results("")
        self._search.setFocus()

    def selected_reference(self) -> FootprintReference | None:
        """Return the selected cached footprint reference."""
        row = self._list.currentRow()
        return self._visible_references[row] if row >= 0 else None

    def _selection_changed(self, row: int) -> None:
        """Enable acceptance and draw the currently highlighted footprint."""
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(row >= 0)
        if row < 0 or row >= len(self._visible_references):
            self._preview.setPixmap(QPixmap())
            self._preview.setText("Select a footprint")
            self._preview_size.setText("Size: —")
            return
        reference = self._visible_references[row]
        try:
            footprint = self._preview_cache.get(reference.identifier)
            if footprint is None:
                footprint = parse_footprint(
                    reference.path.read_bytes(), reference.library
                )
                self._preview_cache[reference.identifier] = footprint
        except (OSError, KiCadFormatError) as error:
            LOGGER.warning(
                "Cannot preview footprint=%s error=%s", reference.identifier, error
            )
            self._preview.setPixmap(QPixmap())
            self._preview.setText("Preview unavailable")
            self._preview_size.setText("Size: —")
            return
        width_mm, height_mm = footprint.dimensions_mm()
        self._preview_size.setText(f"Size: {width_mm:.2f} × {height_mm:.2f} mm")
        size = 340
        canvas = QPixmap(size, size)
        canvas.fill(QColor(32, 33, 36))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(size / 2, size / 2)
        pixels_per_mm = (size - 40) / (2 * max(footprint.radius(), 0.1))
        _paint_footprint(painter, footprint, pixels_per_mm, "top", 0.0, preview=True)
        painter.end()
        self._preview.setPixmap(canvas)

    def _decrease_pad_filter(self) -> None:
        """Decrease the exact pad-count filter, stopping at zero."""
        self._pad_count.setValue(max(0, self._pad_count.value() - 1))

    def _increase_pad_filter(self) -> None:
        """Increase the exact pad-count filter."""
        self._pad_count.setValue(self._pad_count.value() + 1)

    def _refresh_results(self, query: str = "") -> None:
        """Refresh search results and apply the optional exact pad filter."""
        words = query.casefold().split()
        matches = [
            reference
            for reference in self._references
            if all(word in reference.identifier.casefold() for word in words)
        ]
        pad_count = self._pad_count.value()
        if pad_count:
            filtered: list[FootprintReference] = []
            for reference in matches:
                if self._pad_counts.get(reference.identifier) == pad_count:
                    filtered.append(reference)
            matches = filtered
        matches_by_identifier = {
            reference.identifier: reference for reference in matches
        }
        recent = [
            matches_by_identifier[identifier]
            for identifier in self._recent_identifiers
            if identifier in matches_by_identifier
        ]
        recent_set = {reference.identifier for reference in recent}
        ordered = recent + [
            reference for reference in matches if reference.identifier not in recent_set
        ]
        self._visible_references = ordered[: self._MAX_RESULTS]
        self._list.clear()
        for reference in self._visible_references:
            label = (
                f"★ {reference.identifier}"
                if reference.identifier in recent_set
                else reference.identifier
            )
            self._list.addItem(QListWidgetItem(label))
        shown = len(self._visible_references)
        self._status.setText(f"{shown} shown / {len(matches)} matches")
        if shown:
            self._list.setCurrentRow(0)


class KiCadCacheWorker(QObject):  # pylint: disable=too-few-public-methods
    """Prepare the footprint cache away from the GUI event loop."""

    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, cache: KiCadFootprintCache) -> None:
        super().__init__()
        self._cache = cache

    @Slot()
    def run(self) -> None:
        """Run one cache update and report its result."""
        try:
            result = self._cache.ensure_ready()
            self._cache.ensure_pad_count_index(self.progress.emit)
            self.completed.emit(result)
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


def _paint_footprint(  # pylint: disable=too-many-arguments,too-many-positional-arguments,unused-argument
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
    painter.scale(pixels_per_mm, pixels_per_mm)
    pad_pen = QPen(QColor(255, 225, 0, 230 if preview else 180), 1.5)
    pad_pen.setCosmetic(True)
    painter.setPen(pad_pen)
    for pad in footprint.pads:
        painter.save()
        pin_one = pad.number == "1"
        painter.setBrush(
            QColor(
                255,
                235,
                0,
                220 if preview else 150,
            )
            if pin_one
            else QColor(255, 80, 40, 120 if preview else 70)
        )
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


class ImageView(
    QScrollArea
):  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Scrollable image view with mouse-wheel zoom."""

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

    def set_trace_selection(self, net: str | None, origin_id: str | None) -> None:
        """Store visible trace selection for hit testing."""
        self._connection_net = net
        self._connection_origin_id = origin_id

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
        ):
            point = self._label_point(watched, event.position().toPoint())
            hovered = self._pad_at_point(point)
            hovered_id = hovered.pad_id if hovered is not None else None
            if hovered_id != self._hover_pad_id:
                self._hover_pad_id = hovered_id
                self.pad_hovered.emit(hovered)
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
        self._pad_placement = enabled
        if not enabled:
            self._pad_start = None
            self._pad_band.hide()
            self._click_position = None
            self._drag_position = None
            self.unsetCursor()

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
        self._scale = max(0.1, min(self._scale * factor, 20.0))
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
        displayed = self._pixmap.scaled(
            self._pixmap.size() * (fit_scale * self._scale),
            Qt.AspectRatioMode.KeepAspectRatio,
            (
                Qt.TransformationMode.SmoothTransformation
                if smooth
                else Qt.TransformationMode.FastTransformation
            ),
        )
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
            center = QPointF(x, pad.y * (pixmap.height() - 1))
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
        if self._connection_net:
            connected = [pad for pad in pads if pad.net == self._connection_net]
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
                connection_pen = QPen(Qt.GlobalColor.white, 1.5)
                connection_pen.setCosmetic(True)
                painter.setPen(connection_pen)
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
                Qt.GlobalColor.yellow
                if pad.device_id is not None and pad.number == "1"
                else Qt.GlobalColor.red
            )
            pad_pen = QPen(
                (
                    Qt.GlobalColor.white
                    if pad.pad_id == self._connection_origin_id
                    else Qt.GlobalColor.yellow
                ),
                2.0 if pad.pad_id == self._connection_origin_id else 1.0,
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
        self.view_changed.emit()

    def actual_size(self) -> None:
        """Show image at 1:1 source-pixel scale."""
        self._zoom_revision += 1
        self._zoom_render_pending = False
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
        self._zoom_revision += 1
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
        # Keep FIT independent from scrollbar visibility. Otherwise the first
        # zoom step changes its own baseline and makes image/footprint zoom drift.
        viewport = self.maximumViewportSize()
        width_ratio = viewport.width() / self._pixmap.width()
        height_ratio = viewport.height() / self._pixmap.height()
        return min(1.0, width_ratio, height_ratio)


class SchematicCanvas(QWidget):  # pylint: disable=too-many-instance-attributes
    """Paint an electrical schematic using simple IEC-style symbols."""

    layout_changed = Signal(str, float, float)
    net_connection_requested = Signal(object, object)
    terminal_selected = Signal(object)
    terminal_net_edit_requested = Signal(object)
    terminal_menu_requested = Signal(object)
    device_context_requested = Signal(str)
    pan_requested = Signal(int, int)
    connection_mode_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._project: ProjectDocument | None = None
        self._zoom = 1.0
        self._logical_width = 1400
        self._logical_height = 1100
        self._device_centers: dict[str, QPointF] = {}
        self._drag_device_id: str | None = None
        self._drag_offset = QPointF()
        self._terminal_hits: list[tuple[QPointF, tuple[str, str, str | None]]] = []
        self._pending_terminal: tuple[str, str, str | None] | None = None
        self._pending_terminals: list[tuple[str, str, str | None]] = []
        self._pending_wire_start: QPointF | None = None
        self._pending_wire_end: QPointF | None = None
        self._selected_net: str | None = None
        self._pan_position: QPoint | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(self._logical_width, self._logical_height)
        self.setAutoFillBackground(True)

    def set_project(self, project: ProjectDocument | None) -> None:
        """Replace the displayed project and repaint the schematic."""
        self._project = project
        self._resize_for_project()
        self.update()

    def set_selected_net(self, net: str | None) -> None:
        """Highlight one selected electrical connection."""
        self._selected_net = net
        self.update()

    def set_connection_mode(self, enabled: bool) -> None:
        """Use crosshair cursor while editing schematic connections."""
        if enabled:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        else:
            self.unsetCursor()

    def _resize_for_project(self) -> None:
        """Grow the logical sheet so every auto-placed symbol remains visible."""
        devices = self._project.devices if self._project else []
        independent_pads = (
            [pad for pad in self._project.pads if not pad.device_id]
            if self._project
            else []
        )
        device_rows = max(1, math.ceil(len(devices) / 3))
        pad_rows = math.ceil(len(independent_pads) / 6)
        net_count = len(
            {pin.net_id for device in devices for pin in device.pins if pin.net_id}
            | {pad.net for pad in independent_pads if pad.net}
        )
        auto_height = 220 + (device_rows - 1) * 280 + 240 + pad_rows * 44
        saved_bottom = max(
            (
                (device.schematic_y or 0) + self._symbol_size(device)[1] / 2 + 120
                for device in devices
            ),
            default=0,
        )
        self._logical_width = max(1200, 1080 + net_count * 60)
        self._logical_height = max(900, int(auto_height), int(saved_bottom))
        self.resize(
            round(self._logical_width * self._zoom),
            round(self._logical_height * self._zoom),
        )

    def set_zoom(self, zoom: float) -> None:
        """Resize the canvas while keeping its schematic geometry vector-based."""
        self._zoom = max(0.5, min(4.0, zoom))
        self.resize(
            round(self._logical_width * self._zoom),
            round(self._logical_height * self._zoom),
        )
        self.update()

    def _center_for_device(self, device: Device, index: int) -> QPointF:
        """Return persisted schematic position or a stable automatic position."""
        if device.schematic_x is not None and device.schematic_y is not None:
            return QPointF(device.schematic_x, device.schematic_y)
        columns = 3
        return QPointF(220 + (index % columns) * 300, 180 + (index // columns) * 280)

    @classmethod
    def _symbol_size(cls, device: Device) -> tuple[float, float]:
        """Return the collision/display size of one schematic symbol."""
        if cls._symbol_kind(device) == "uc":
            pins = max(1, len(device.pins))
            side_pins = max(1, math.ceil(pins / 4))
            side = max(120.0, side_pins * 22.0 + 30.0)
            return side, side
        if cls._symbol_kind(device) == "connector":
            return 150.0, max(100.0, len(device.pins) * 20.0 + 30.0)
        if cls._symbol_kind(device) == "transistor":
            return 150.0, 130.0
        if cls._symbol_kind(device) in {"resistor", "capacitor"}:
            return 110.0, 80.0
        return 150.0, 120.0

    @classmethod
    def _device_bounds(cls, device: Device, center: QPointF) -> QRectF:
        """Return a padded collision rectangle for one component."""
        width, height = cls._symbol_size(device)
        return QRectF(
            center.x() - width / 2 - 12,
            center.y() - height / 2 - 12,
            width + 24,
            height + 24,
        )

    @staticmethod
    def _uc_endpoints(center: QPointF, pin_count: int, side: float) -> list[QPointF]:
        """Place UC pins on all four square edges, never inside the body."""
        side_pins = max(1, math.ceil(pin_count / 4))
        points: list[QPointF] = []
        half = side / 2
        for index in range(pin_count):
            edge = index // side_pins
            offset = (index % side_pins + 1) / (side_pins + 1) * side - half
            if edge == 0:
                points.append(QPointF(center.x() - half, center.y() + offset))
            elif edge == 1:
                points.append(QPointF(center.x() + offset, center.y() - half))
            elif edge == 2:
                points.append(QPointF(center.x() + half, center.y() - offset))
            else:
                points.append(QPointF(center.x() - offset, center.y() + half))
        return points

    @staticmethod
    def _pin_label(pin: ComponentPin) -> str:
        """Return a compact pin number/name suitable for schematic symbols."""
        semantic = ""
        if pin.function and pin.function != f"Pin {pin.number}":
            semantic = pin.function
        elif pin.pin_id != pin.number:
            semantic = pin.pin_id
        return f"{pin.number} {semantic}".strip()

    def _draw_uc_pins(
        self,
        painter: QPainter,
        pins: list[ComponentPin],
        side: float,
    ) -> list[QPointF]:
        """Draw UC pin names inside and terminal wires outside the square."""
        half = side / 2
        edge_points = self._uc_endpoints(QPointF(0, 0), len(pins), side)
        endpoints: list[QPointF] = []
        metrics = painter.fontMetrics()
        for pin, edge in zip(pins, edge_points, strict=True):
            label = self._pin_label(pin)
            if math.isclose(edge.x(), -half):
                outward = QPointF(-1, 0)
                painter.drawText(int(-half + 7), int(edge.y() + 5), label)
            elif math.isclose(edge.x(), half):
                outward = QPointF(1, 0)
                painter.drawText(
                    int(half - metrics.horizontalAdvance(label) - 7),
                    int(edge.y() + 5),
                    label,
                )
            elif math.isclose(edge.y(), -half):
                outward = QPointF(0, -1)
                painter.save()
                painter.translate(edge.x() + 5, -half + 7)
                painter.rotate(90)
                painter.drawText(0, 0, label)
                painter.restore()
            else:
                outward = QPointF(0, 1)
                painter.save()
                painter.translate(edge.x() - 5, half - 7)
                painter.rotate(-90)
                painter.drawText(0, 0, label)
                painter.restore()
            endpoint = edge + outward * 24
            painter.drawLine(edge, endpoint)
            endpoints.append(endpoint)
        return endpoints

    def _device_at(self, point: QPointF) -> str | None:
        """Return the component under a logical canvas point."""
        if self._project is None:
            return None
        devices = {device.device_id: device for device in self._project.devices}
        for device_id, center in reversed(tuple(self._device_centers.items())):
            device = devices.get(device_id)
            if device is not None and self._device_bounds(device, center).contains(
                point
            ):
                return device_id
        return None

    def _terminal_at(
        self, point: QPointF
    ) -> tuple[tuple[str, str, str | None], QPointF] | None:
        """Return the nearest schematic terminal around a logical point."""
        candidates = [
            (QLineF(point, terminal_point).length(), terminal, terminal_point)
            for terminal_point, terminal in self._terminal_hits
            if QLineF(point, terminal_point).length() <= 18.0
        ]
        if not candidates:
            return None
        _distance, terminal, terminal_point = min(candidates, key=lambda item: item[0])
        return terminal, terminal_point

    def _clear_pending_wire(self) -> None:
        """Cancel the interactive net wire preview."""
        self._pending_terminal = None
        self._pending_terminals = []
        self._pending_wire_start = None
        self._pending_wire_end = None
        self.set_connection_mode(False)
        self.connection_mode_changed.emit(False)
        self.update()

    def finish_connection(self) -> bool:
        """Leave connector mode after its links were created incrementally."""
        if not self._pending_terminals:
            return False
        self._clear_pending_wire()
        return True

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Start dragging a schematic component."""
        point = event.position() / self._zoom
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            hit = self._terminal_at(point)
            if hit is None:
                self._clear_pending_wire()
                return
            terminal, terminal_point = hit
            if self._pending_terminal is None:
                self.set_connection_mode(True)
                self.connection_mode_changed.emit(True)
                self._pending_terminal = terminal
                self._pending_terminals = [terminal]
                self._pending_wire_start = terminal_point
                self._pending_wire_end = point
            elif terminal not in self._pending_terminals:
                self._pending_terminals.append(terminal)
                self._pending_terminal = terminal
                self._pending_wire_start = terminal_point
                self._pending_wire_end = point
                self.net_connection_requested.emit(self._pending_terminals[0], terminal)
            self.setFocus()
            event.accept()
            self.update()
            return
        terminal_hit = self._terminal_at(point)
        if (
            event.button() == Qt.MouseButton.RightButton
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            and terminal_hit is not None
        ):
            terminal, _terminal_point = terminal_hit
            self.terminal_net_edit_requested.emit(terminal)
            event.accept()
            return
        if (
            event.button() == Qt.MouseButton.RightButton
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
            and terminal_hit is not None
        ):
            terminal, _terminal_point = terminal_hit
            self.terminal_menu_requested.emit(terminal)
            event.accept()
            return
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            if terminal_hit is not None:
                terminal, _terminal_point = terminal_hit
                if self._pending_terminals:
                    if terminal not in self._pending_terminals:
                        self._pending_terminals.append(terminal)
                        self.net_connection_requested.emit(
                            self._pending_terminals[0], terminal
                        )
                    self._pending_terminal = terminal
                    self._pending_wire_start = terminal_hit[1]
                    self._pending_wire_end = point
                    self.update()
                else:
                    self.terminal_selected.emit(terminal)
                event.accept()
                return
        device_id = self._device_at(point)
        if event.button() == Qt.MouseButton.RightButton and device_id is not None:
            self.device_context_requested.emit(device_id)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if device_id is None:
            self._pan_position = event.globalPosition().toPoint()
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            event.accept()
            return
        self._drag_device_id = device_id
        self._drag_offset = point - self._device_centers[device_id]
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        """Move the selected component on the schematic grid."""
        if self._pending_terminal is not None:
            self._pending_wire_end = event.position() / self._zoom
            self.update()
            event.accept()
            return
        if self._pan_position is not None:
            current = event.globalPosition().toPoint()
            delta = current - self._pan_position
            self._pan_position = current
            self.pan_requested.emit(delta.x(), delta.y())
            event.accept()
            return
        if self._drag_device_id is None or self._project is None:
            return
        point = event.position() / self._zoom - self._drag_offset
        device = next(
            (
                item
                for item in self._project.devices
                if item.device_id == self._drag_device_id
            ),
            None,
        )
        if device is None:
            return
        width, height = self._symbol_size(device)
        point.setX(
            max(width / 2 + 12, min(self._logical_width - width / 2 - 12, point.x()))
        )
        point.setY(
            max(height / 2 + 12, min(self._logical_height - height / 2 - 12, point.y()))
        )
        for other in self._project.devices:
            if other.device_id == device.device_id:
                continue
            other_center = self._device_centers.get(other.device_id)
            if other_center is not None and self._device_bounds(
                other, other_center
            ).intersects(self._device_bounds(device, point)):
                return
        self._device_centers[self._drag_device_id] = point
        self._project.devices = [
            (
                replace(device, schematic_x=point.x(), schematic_y=point.y())
                if device.device_id == self._drag_device_id
                else device
            )
            for device in self._project.devices
        ]
        self.update()
        self.layout_changed.emit(self._drag_device_id, point.x(), point.y())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """Finish component dragging."""
        self._drag_device_id = None
        self._pan_position = None
        self.unsetCursor()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """Finish connector mode explicitly with Escape."""
        if event.key() == Qt.Key.Key_Escape and self.finish_connection():
            event.accept()
            return
        super().keyPressEvent(event)

    @staticmethod
    def _symbol_kind(device: Device) -> str:
        """Classify a component from KiCad metadata and its reference."""
        family = _footprint_family(device.footprint_library).casefold().replace(" ", "")
        name = device.footprint_name.casefold()
        reference = device.reference.casefold()
        object_type = device.object_type.casefold().replace(" ", "")
        family = object_type or family
        if "connector" in family or "terminal" in family or reference.startswith("j"):
            return "connector"
        if "battery" in family or reference.startswith("bt"):
            return "battery"
        if "switch" in family or reference.startswith("sw"):
            return "switch"
        if "transistor" in family or "mosfet" in name or reference.startswith("q"):
            return "transistor"
        if "diode" in family or "led" in family or reference.startswith("d"):
            return "led" if "led" in family or "led" in name else "diode"
        if "capacitor" in family or "capacitor" in name or reference.startswith("c"):
            return "capacitor"
        if "resistor" in family or "resistor" in name or reference.startswith("r"):
            return "resistor"
        return "uc"

    @staticmethod
    def _draw_resistor(painter: QPainter, center: QPointF) -> None:
        """Draw a horizontal resistor symbol."""
        x, y = center.x(), center.y()
        painter.drawLine(QPointF(x - 58, y), QPointF(x - 34, y))
        painter.drawLine(QPointF(x + 34, y), QPointF(x + 58, y))
        points = [QPointF(x - 34, y)]
        for offset, height in ((-24, -12), (-12, 12), (0, -12), (12, 12), (24, 0)):
            points.append(QPointF(x + offset, y + height))
        painter.drawPolyline(points)

    @staticmethod
    def _draw_capacitor(painter: QPainter, center: QPointF) -> None:
        """Draw a horizontal capacitor symbol."""
        x, y = center.x(), center.y()
        painter.drawLine(QPointF(x - 58, y), QPointF(x - 8, y))
        painter.drawLine(QPointF(x + 8, y), QPointF(x + 58, y))
        painter.drawLine(QPointF(x - 8, y - 22), QPointF(x - 8, y + 22))
        painter.drawLine(QPointF(x + 8, y - 22), QPointF(x + 8, y + 22))

    @staticmethod
    def _draw_diode(painter: QPainter, center: QPointF, led: bool = False) -> None:
        """Draw a diode or LED symbol."""
        x, y = center.x(), center.y()
        painter.drawLine(QPointF(x - 58, y), QPointF(x - 18, y))
        painter.drawLine(QPointF(x + 18, y), QPointF(x + 58, y))
        painter.drawPolygon(
            [QPointF(x - 18, y - 22), QPointF(x - 18, y + 22), QPointF(x + 18, y)]
        )
        painter.drawLine(QPointF(x + 18, y - 24), QPointF(x + 18, y + 24))
        if led:
            painter.drawLine(QPointF(x - 4, y - 28), QPointF(x + 8, y - 40))
            painter.drawLine(QPointF(x + 8, y - 40), QPointF(x + 5, y - 32))
            painter.drawLine(QPointF(x + 12, y - 24), QPointF(x + 24, y - 36))
            painter.drawLine(QPointF(x + 24, y - 36), QPointF(x + 21, y - 28))

    @staticmethod
    def _draw_battery(painter: QPainter, center: QPointF) -> None:
        """Draw a two-cell battery symbol."""
        x, y = center.x(), center.y()
        painter.drawLine(QPointF(x - 58, y), QPointF(x - 18, y))
        painter.drawLine(QPointF(x + 18, y), QPointF(x + 58, y))
        painter.drawLine(QPointF(x - 12, y - 28), QPointF(x - 12, y + 28))
        painter.drawLine(QPointF(x - 2, y - 16), QPointF(x - 2, y + 16))
        painter.drawLine(QPointF(x + 2, y - 16), QPointF(x + 2, y + 16))
        painter.drawLine(QPointF(x + 12, y - 28), QPointF(x + 12, y + 28))
        painter.drawText(int(x - 8), int(y - 34), "+")

    @staticmethod
    def _draw_switch(painter: QPainter, center: QPointF) -> None:
        """Draw an open switch symbol."""
        x, y = center.x(), center.y()
        painter.drawLine(QPointF(x - 58, y), QPointF(x - 30, y))
        painter.drawLine(QPointF(x + 30, y), QPointF(x + 58, y))
        painter.drawEllipse(QPointF(x - 30, y - 3), 3, 3)
        painter.drawEllipse(QPointF(x + 30, y - 3), 3, 3)
        painter.drawLine(QPointF(x - 30, y), QPointF(x + 20, y - 24))

    @staticmethod
    def _draw_connector(
        painter: QPainter, center: QPointF, pin_count: int
    ) -> list[QPointF]:
        """Draw a connector block with one-sided numbered pins."""
        x, y = center.x(), center.y()
        height = max(54, pin_count * 20 + 18)
        box = QRectF(x - 42, y - height / 2, 84, height)
        painter.drawRect(box)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, "CNX")
        endpoints = []
        for index in range(pin_count):
            pin_y = y + (index - (pin_count - 1) / 2) * 20
            painter.drawLine(QPointF(x + 42, pin_y), QPointF(x + 68, pin_y))
            endpoints.append(QPointF(x + 68, pin_y))
        return endpoints

    @staticmethod
    def _draw_transistor(painter: QPainter, center: QPointF) -> list[QPointF]:
        """Draw a compact BJT-style transistor symbol with three terminals."""
        x, y = center.x(), center.y()
        painter.drawEllipse(center, 30, 30)
        painter.drawLine(QPointF(x - 58, y), QPointF(x - 20, y))
        painter.drawLine(QPointF(x + 5, y - 20), QPointF(x + 58, y - 48))
        painter.drawLine(QPointF(x + 5, y + 20), QPointF(x + 58, y + 48))
        painter.drawLine(QPointF(x - 4, y - 20), QPointF(x - 4, y + 20))
        return [
            QPointF(x - 58, y),
            QPointF(x + 58, y - 48),
            QPointF(x + 58, y + 48),
        ]

    def _draw_device(
        self,
        painter: QPainter,
        device: Device,
        center: QPointF,
        net_points: dict[str, list[tuple[QPointF, QPointF]]],
    ) -> None:
        """Draw one symbol and collect its connected terminal positions."""
        pins = device.pins or [ComponentPin("?", "unmapped")]
        kind = self._symbol_kind(device)
        painter.setPen(QPen(QColor("#e7edf5"), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.save()
        painter.translate(center)
        painter.rotate(device.schematic_rotation)
        origin = QPointF(0, 0)
        if kind == "resistor":
            self._draw_resistor(painter, origin)
            endpoints = [
                QPointF(-58, 0),
                QPointF(58, 0),
            ]
        elif kind == "capacitor":
            self._draw_capacitor(painter, origin)
            endpoints = [
                QPointF(-58, 0),
                QPointF(58, 0),
            ]
        elif kind in {"diode", "led"}:
            self._draw_diode(painter, origin, led=kind == "led")
            endpoints = [
                QPointF(-58, 0),
                QPointF(58, 0),
            ]
        elif kind == "battery":
            self._draw_battery(painter, origin)
            endpoints = [
                QPointF(-58, 0),
                QPointF(58, 0),
            ]
        elif kind == "switch":
            self._draw_switch(painter, origin)
            endpoints = [
                QPointF(-58, 0),
                QPointF(58, 0),
            ]
        elif kind == "connector":
            endpoints = self._draw_connector(painter, origin, len(pins))
        elif kind == "transistor":
            endpoints = self._draw_transistor(painter, origin)
        else:
            side = self._symbol_size(device)[0]
            box = QRectF(-side / 2, -side / 2, side, side)
            painter.drawRect(box)
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, "UC")
            endpoints = self._draw_uc_pins(painter, pins, side)
        transform = QTransform()
        transform.translate(center.x(), center.y())
        transform.rotate(device.schematic_rotation)
        endpoints = [transform.map(endpoint) for endpoint in endpoints]
        painter.restore()
        painter.setPen(QColor("#f2f6fa"))
        painter.drawText(int(center.x() - 70), int(center.y() - 48), device.reference)
        if device.value:
            painter.setPen(QColor("#aeb7c4"))
            painter.drawText(int(center.x() - 70), int(center.y() + 58), device.value)
        for index, pin in enumerate(pins):
            endpoint = endpoints[min(index, len(endpoints) - 1)]
            self._terminal_hits.append(
                (endpoint, ("pin", device.device_id, pin.number))
            )
            painter.setPen(QColor("#c5d2dd"))
            delta_x = endpoint.x() - center.x()
            delta_y = endpoint.y() - center.y()
            if kind != "uc":
                if abs(delta_x) >= abs(delta_y):
                    text_x = endpoint.x() - (74 if delta_x < 0 else -8)
                    text_y = endpoint.y() - 5
                else:
                    text_x = endpoint.x() - 12
                    text_y = endpoint.y() - (10 if delta_y < 0 else -20)
                painter.drawText(int(text_x), int(text_y), self._pin_label(pin))
            if pin.net_id:
                if abs(delta_x) >= abs(delta_y):
                    outward = QPointF(-1 if delta_x < 0 else 1, 0)
                else:
                    outward = QPointF(0, -1 if delta_y < 0 else 1)
                net_points.setdefault(pin.net_id, []).append((endpoint, outward))

    def paintEvent(self, event) -> None:  # noqa: N802
        """Draw real component symbols, terminals, and net wires."""
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#20242b"))
        painter.scale(self._zoom, self._zoom)
        painter.setClipRect(QRectF(0, 0, self._logical_width, self._logical_height))
        if self._project is None:
            painter.setPen(QColor("#aeb7c4"))
            painter.drawText(
                QRect(0, 0, self._logical_width, self._logical_height),
                Qt.AlignmentFlag.AlignCenter,
                "No project",
            )
            return
        devices = sorted(
            self._project.devices,
            key=lambda device: (
                0 if len(device.pins) > 4 else 1,
                _reference_sort_key(device.reference),
            ),
        )
        pads = sorted(self._project.pads, key=lambda pad: pad.name)
        self._terminal_hits = []
        net_names = sorted(
            {pin.net_id for device in devices for pin in device.pins if pin.net_id}
            | {pad.net for pad in pads if pad.net}
        )
        painter.setPen(QColor("#e7edf5"))
        painter.drawText(18, 28, "Schematic")
        if not devices and not pads:
            painter.setPen(QColor("#aeb7c4"))
            painter.drawText(18, 62, "Place components or pads to build the schematic.")
            return

        net_points: dict[str, list[tuple[QPointF, QPointF]]] = {}
        for index, device in enumerate(devices):
            center = self._center_for_device(device, index)
            self._device_centers[device.device_id] = center
            self._draw_device(
                painter,
                device,
                center,
                net_points,
            )
        independent_pads = [pad for pad in pads if not pad.device_id]
        pad_start_y = 220 + math.ceil(len(devices) / 3) * 280
        for index, pad in enumerate(independent_pads):
            pad_point = QPointF(
                180 + (index % 6) * 190,
                pad_start_y + (index // 6) * 54,
            )
            painter.setPen(QPen(QColor("#e7edf5"), 2))
            painter.drawRect(QRectF(pad_point.x() - 18, pad_point.y() - 10, 36, 20))
            painter.drawText(int(pad_point.x() - 12), int(pad_point.y() - 16), pad.name)
            self._terminal_hits.append(
                (pad_point + QPointF(18, 0), ("pad", pad.pad_id, None))
            )
            if pad.net:
                net_points.setdefault(pad.net, []).append(
                    (pad_point + QPointF(18, 0), QPointF(1, 0))
                )
        painter.setPen(QPen(QColor("#e4b363"), 2))
        painter.setBrush(QColor("#e4b363"))
        for name in net_names:
            selected = name == self._selected_net
            painter.setPen(
                QPen(
                    QColor("#66c2ff") if selected else QColor("#e4b363"),
                    6 if selected else 4,
                )
            )
            painter.setBrush(QColor("#66c2ff") if selected else QColor("#e4b363"))
            points = net_points.get(name, [])
            if not points:
                continue
            stubs = []
            for point, outward in points:
                stub = point + outward * 26
                painter.drawLine(point, stub)
                stubs.append(stub)
            if len(stubs) == 2:
                middle_x = (stubs[0].x() + stubs[1].x()) / 2
                painter.drawLine(stubs[0], QPointF(middle_x, stubs[0].y()))
                painter.drawLine(
                    QPointF(middle_x, stubs[0].y()),
                    QPointF(middle_x, stubs[1].y()),
                )
                painter.drawLine(QPointF(middle_x, stubs[1].y()), stubs[1])
                painter.drawEllipse(QPointF(middle_x, stubs[0].y()), 3, 3)
                painter.drawText(int(middle_x + 7), int(stubs[0].y() - 6), name)
            else:
                for stub, (_point, outward) in zip(stubs, points, strict=True):
                    text_x = stub.x() + (7 if outward.x() >= 0 else -48)
                    text_y = stub.y() + (16 if outward.y() > 0 else -6)
                    painter.drawText(int(text_x), int(text_y), name)

        if self._pending_wire_start is not None and self._pending_wire_end is not None:
            start = self._pending_wire_start
            end = self._pending_wire_end
            middle_x = (start.x() + end.x()) / 2
            painter.setPen(QPen(QColor("#66c2ff"), 4, Qt.PenStyle.DashLine))
            painter.drawLine(start, QPointF(middle_x, start.y()))
            painter.drawLine(QPointF(middle_x, start.y()), QPointF(middle_x, end.y()))
            painter.drawLine(QPointF(middle_x, end.y()), end)

        orphan_y = 82 + len(devices) * 150
        for pad in pads:
            if pad.device_id or pad.net:
                continue
            painter.setPen(QColor("#aeb7c4"))
            painter.drawText(24, orphan_y, f"Pad {pad.name} (unconnected)")
            orphan_y += 22


class SchematicView(QScrollArea):
    """Scrollable, zoomable schematic viewport."""

    layout_changed = Signal(str, float, float)
    net_connection_requested = Signal(object, object)
    terminal_selected = Signal(object)
    terminal_net_edit_requested = Signal(object)
    terminal_menu_requested = Signal(object)
    device_context_requested = Signal(str)
    connection_mode_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._zoom = 1.0
        self._canvas = SchematicCanvas()
        self._canvas.layout_changed.connect(self.layout_changed)
        self._canvas.net_connection_requested.connect(self.net_connection_requested)
        self._canvas.terminal_selected.connect(self.terminal_selected)
        self._canvas.terminal_net_edit_requested.connect(
            self.terminal_net_edit_requested
        )
        self._canvas.terminal_menu_requested.connect(self.terminal_menu_requested)
        self._canvas.device_context_requested.connect(self.device_context_requested)
        self._canvas.connection_mode_changed.connect(self.connection_mode_changed)
        self._canvas.pan_requested.connect(self._pan_by)
        self.setWidget(self._canvas)
        self.setWidgetResizable(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setMinimumSize(520, 320)
        self.setStyleSheet("QScrollArea { background: #20242b; }")
        self.viewport().setStyleSheet("background: #20242b;")
        self.setToolTip(
            "Wheel/pinch: zoom; Shift+click: connector mode; Esc: finish; "
            "Shift+right-click: edit net"
        )

    def set_project(self, project: ProjectDocument | None) -> None:
        """Set the project rendered by the schematic canvas."""
        self._canvas.set_project(project)

    def set_selected_net(self, net: str | None) -> None:
        """Highlight one selected net in the schematic canvas."""
        self._canvas.set_selected_net(net)

    def set_connection_mode(self, enabled: bool) -> None:
        """Set schematic cursor for connection editing."""
        self._canvas.set_connection_mode(enabled)

    def finish_connection(self) -> bool:
        """Finish a pending Shift-click connector session."""
        return self._canvas.finish_connection()

    def event(self, event) -> bool:  # noqa: N802
        """Zoom schematic with native trackpad pinch gestures."""
        if (
            event.type() == QEvent.Type.NativeGesture
            and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture
        ):
            self._zoom_by(max(0.01, 1.0 + event.value()))
            return True
        return super().event(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        """Zoom around the mouse position with the wheel."""
        if event.angleDelta().y() == 0:
            super().wheelEvent(event)
            return
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        self._zoom_by(factor, event.position().toPoint())
        event.accept()

    def _zoom_by(self, factor: float, cursor: QPoint | None = None) -> None:
        """Apply one zoom step around the cursor or viewport center."""
        old_zoom = self._zoom
        self._zoom = max(0.5, min(4.0, self._zoom * factor))
        if self._zoom == old_zoom:
            return
        cursor = cursor or QPoint(
            self.viewport().width() // 2, self.viewport().height() // 2
        )
        source_x = (self.horizontalScrollBar().value() + cursor.x()) / old_zoom
        source_y = (self.verticalScrollBar().value() + cursor.y()) / old_zoom
        self._canvas.set_zoom(self._zoom)
        self.horizontalScrollBar().setValue(round(source_x * self._zoom - cursor.x()))
        self.verticalScrollBar().setValue(round(source_y * self._zoom - cursor.y()))

    def _pan_by(self, delta_x: int, delta_y: int) -> None:
        """Pan the schematic when dragging outside terminals/components."""
        self.horizontalScrollBar().setValue(
            self.horizontalScrollBar().value() - delta_x
        )
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta_y)


class MainWindow(
    QMainWindow
):  # pylint: disable=too-many-instance-attributes,too-many-locals,too-many-statements
    """Minimal project and board-picture workspace."""

    def __init__(  # pylint: disable=too-many-statements
        self,
        show_startup: bool = True,
        footprint_cache: KiCadFootprintCache | None = None,
        settings: QSettings | None = None,
    ) -> None:
        super().__init__()
        self.project: ProjectDocument | None = None
        self.store: ProjectStore | None = None
        self._dirty = False
        self._syncing_views = False
        self._pending_pad: Pad | None = None
        self._pending_device: PendingDevice | None = None
        self._add_device_pending = False
        self._selected_net: str | None = None
        self._selected_pad_id: str | None = None
        self._selected_schematic_terminal: tuple[str, str, str | None] | None = None
        self._pending_board_connection_pads: list[str] = []
        self._pads_visible = True
        self._pad_display_mode = "both"
        self._pad_refresh_pending = False
        self._pending_pad_view_state: tuple[float, float, float] | None = None
        self._image_cache: dict[str, QPixmap] = {}
        self._device_footprint_cache: dict[str, Footprint] = {}
        self._settings = (
            settings if settings is not None else QSettings("Tnasrevner", "Tnasrevner")
        )
        self._footprint_cache = footprint_cache or KiCadFootprintCache(
            _application_data_directory(create=False) / "kicad-footprints"
        )
        self._kicad_cache_thread: QThread | None = None
        self._kicad_cache_worker: KiCadCacheWorker | None = None
        self._kicad_progress: QProgressDialog | None = None
        self._kicad_revision: str | None = None
        self._close_when_cache_finishes = False
        self._net_dialog: QInputDialog | None = None
        self._device_value_dialog: QInputDialog | None = None
        self._pad_menu: QMenu | None = None
        self._last_view_key: int | None = None
        self._last_view_time = 0.0
        self.setWindowIcon(_application_icon())
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
        self._net_table = QTableWidget(0, 6)
        self._net_table.setHorizontalHeaderLabels(
            ["Pad", "Net", "Pin", "Function", "Component", "Value"]
        )
        self._net_table.cellChanged.connect(self._net_table_cell_changed)
        self._tabs.addTab(self._net_table, "Nets")
        self._bom_table = QTableWidget(0, 6)
        self._bom_table.setHorizontalHeaderLabels(
            [
                "Component",
                "Type",
                "Footprint",
                "Value",
                "Description",
                "Datasheet",
            ]
        )
        self._bom_table.cellChanged.connect(self._bom_table_cell_changed)
        self._bom_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._bom_table.customContextMenuRequested.connect(self._show_bom_menu)
        self._tabs.addTab(self._bom_table, "BOM")
        self._schematic_view = SchematicView()
        self._schematic_view.layout_changed.connect(self._schematic_layout_changed)
        self._schematic_view.net_connection_requested.connect(
            self._connect_schematic_terminals
        )
        self._schematic_view.terminal_selected.connect(self._select_schematic_terminal)
        self._schematic_view.terminal_net_edit_requested.connect(
            self._edit_schematic_terminal_net
        )
        self._schematic_view.terminal_menu_requested.connect(
            self._show_schematic_terminal_menu
        )
        self._schematic_view.device_context_requested.connect(
            self._show_schematic_device_menu
        )
        self._schematic_view.connection_mode_changed.connect(self._set_connection_mode)
        self._tabs.addTab(self._schematic_view, "Schematic")
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
            view.trace_menu_requested.connect(
                lambda x, y, side=side: self._defer_trace_menu(side, x, y)
            )
            view.pad_connection_requested.connect(
                lambda x, y, side=side: self._select_board_connection_pad(side, x, y)
            )
            view.device_placed.connect(
                lambda x, y, side=side: self._place_device(side, x, y)
            )
            view.device_rotated.connect(self._rotate_pending_device)
            view.delete_requested.connect(
                lambda x, y, side=side: self._delete_at(side, x, y)
            )
            view.delete_mode_changed.connect(self._delete_mode_changed)
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
            view.trace_menu_requested.connect(
                lambda x, y, side=side: self._defer_trace_menu(side, x, y)
            )
            view.pad_connection_requested.connect(
                lambda x, y, side=side: self._select_board_connection_pad(side, x, y)
            )
            view.device_placed.connect(
                lambda x, y, side=side: self._place_device(side, x, y)
            )
            view.device_rotated.connect(self._rotate_pending_device)
            view.delete_requested.connect(
                lambda x, y, side=side: self._delete_at(side, x, y)
            )
            view.delete_mode_changed.connect(self._delete_mode_changed)
        for view in (
            *self._views.values(),
            *self._side_views.values(),
            self._overlay_view,
        ):
            view.ruler_measured.connect(self._show_ruler_measurement)
            view.pad_hovered.connect(self._show_pad_hover)
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

    def _create_tool_palette(  # pylint: disable=too-many-locals,too-many-statements
        self,
    ) -> None:
        """Create the right-side view control palette."""
        dock = QDockWidget("Tools", self)
        dock.setObjectName("toolsDock")
        panel = QWidget(dock)
        layout = QVBoxLayout(panel)

        def add_button(
            text: str,
            icon: QStyle.StandardPixmap,
            tooltip: str,
            callback: Callable[[], None],
        ) -> QPushButton:
            """Add icon, tooltip, and hover feedback to palette button."""
            button = QPushButton(panel)
            button.setIcon(self.style().standardIcon(icon))
            button.setAccessibleName(text)
            button.setObjectName(
                f"tool{''.join(character for character in text.title() if character.isalnum())}"
            )
            button.setFixedSize(40, 36)
            button.setToolTip(tooltip)
            button.setStatusTip(tooltip)
            button.clicked.connect(lambda _checked=False, action=callback: action())
            layout.addWidget(button)
            return button

        add_button(
            "1:1",
            QStyle.StandardPixmap.SP_ComputerIcon,
            "Actual image size",
            self._actual_size,
        )
        add_button(
            "FIT",
            QStyle.StandardPixmap.SP_DesktopIcon,
            "Fit image in view",
            self._fit_images,
        )
        add_button(
            "Rotate 90°",
            QStyle.StandardPixmap.SP_BrowserReload,
            "Rotate the complete board 90 degrees",
            self._rotate_board_90,
        )
        center_button = add_button(
            "Center",
            QStyle.StandardPixmap.SP_DialogResetButton,
            "Center image",
            self._center_images,
        )
        center_button.setIcon(_center_tool_icon())
        ruler_button = add_button(
            "Ruler",
            QStyle.StandardPixmap.SP_FileDialogContentsView,
            "Measure a distance in millimeters using the image scale",
            self._toggle_ruler,
        )
        ruler_button.setCheckable(True)
        self._ruler_button = ruler_button
        show_pads_button = QPushButton(panel)
        show_pads_button.setAccessibleName("Pad display mode")
        show_pads_button.setObjectName("toolShowPads")
        show_pads_button.setFixedSize(40, 36)
        show_pads_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogYesButton)
        )
        show_pads_button.setToolTip("Pad + image")
        show_pads_button.setStatusTip(show_pads_button.toolTip())
        show_pads_button.clicked.connect(self._cycle_pad_display_mode)
        self._show_pads_button = show_pads_button
        layout.addWidget(show_pads_button)
        layout.addSpacing(12)
        device_button = add_button(
            "Add Footprint",
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
            "Select and place a KiCad footprint",
            self.add_device,
        )
        device_button.setIcon(
            QIcon.fromTheme(
                "list-add",
                self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder),
            )
        )
        self._add_device_button = device_button
        pad_button = add_button(
            "Add Pad",
            QStyle.StandardPixmap.SP_FileDialogNewFolder,
            "Create a pad on the Top or Bottom image",
            self.create_pad,
        )
        pad_button.setIcon(_pad_tool_icon())
        delete_button = add_button(
            "Delete",
            QStyle.StandardPixmap.SP_TrashIcon,
            "Delete pads or footprints by clicking them",
            self._toggle_delete_mode,
        )
        delete_button.setCheckable(True)
        self._delete_button = delete_button
        add_button(
            "Log file",
            QStyle.StandardPixmap.SP_FileDialogInfoView,
            "Show diagnostic log file path",
            self._show_log_path,
        )
        save_button = add_button(
            "Save",
            QStyle.StandardPixmap.SP_DialogSaveButton,
            "Save project",
            self.save_project,
        )
        save_button.setIcon(_save_tool_icon())
        layout.addStretch()
        add_button(
            "Image",
            QStyle.StandardPixmap.SP_DialogOpenButton,
            "Choose Top or Bottom image: load, resize, or remove",
            self.manage_picture,
        )
        add_button(
            "Quit",
            QStyle.StandardPixmap.SP_DialogCloseButton,
            "Quit Tnasrevner",
            self.close,
        )
        self._net_mode_label = QLabel("Net edition - Esc to Stop", panel)
        self._net_mode_label.setObjectName("netModeLabel")
        self._net_mode_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._net_mode_label.setStyleSheet("color: #66c2ff; font-weight: 600;")
        self._net_mode_label.setVisible(False)
        layout.addWidget(self._net_mode_label)
        panel.setStyleSheet(
            "QPushButton { padding: 6px 8px; }"
            "QPushButton:hover { background: palette(highlight); "
            "color: palette(highlighted-text); }"
        )
        panel.setLayout(layout)
        dock.setWidget(panel)
        self._tools_dock = dock
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def _rotate_board_90(self) -> None:
        """Rotate both board images and all placed geometry by 90 degrees."""
        if not self.project or not self.store or not self.project.images:
            return
        for asset in self.project.images:
            pixmap = self._base_pixmap_for_asset(asset.side)
            if pixmap.isNull():
                continue
            rotated = pixmap.transformed(QTransform().rotate(90))
            self.store.write_asset(asset.path, self._pixmap_bytes(rotated))

        def rotate_point(x: float, y: float) -> tuple[float, float]:
            # QPixmap/QPainter +90° maps (x, y) to (1-y, x).
            return 1.0 - y, x

        def rotate_rectangle(
            x: float, y: float, width: float, height: float
        ) -> tuple[float, float, float, float]:
            return 1.0 - y - height, x, height, width

        rotated_images = []
        for asset in self.project.images:
            line = asset.calibration_line
            if line is not None:
                start = rotate_point(line[0], line[1])
                end = rotate_point(line[2], line[3])
                line = (*start, *end)
            rotated_images.append(replace(asset, calibration_line=line))
        self.project.images = rotated_images
        self.project.pads = [
            replace(
                pad,
                x=rotate_rectangle(pad.x, pad.y, pad.width, pad.height)[0],
                y=rotate_rectangle(pad.x, pad.y, pad.width, pad.height)[1],
                width=rotate_rectangle(pad.x, pad.y, pad.width, pad.height)[2],
                height=rotate_rectangle(pad.x, pad.y, pad.width, pad.height)[3],
                rotation=(pad.rotation + 90.0) % 360.0,
            )
            for pad in self.project.pads
        ]
        self.project.devices = [
            replace(
                device,
                x=rotate_point(device.x, device.y)[0],
                y=rotate_point(device.x, device.y)[1],
                rotation=(device.rotation + 90.0) % 360.0,
                schematic_rotation=(device.schematic_rotation + 90.0) % 360.0,
            )
            for device in self.project.devices
        ]
        self._image_cache.clear()
        for side in ("top", "bottom"):
            self._rebuild_device_pads(side, self._base_pixmap_for_asset(side))
        self._dirty = True
        current_tab = self._tabs.currentIndex()
        view_state = self._active_views()[0].view_state()
        self._refresh_views()
        self._tabs.setCurrentIndex(current_tab)
        self._apply_active_view_state(view_state)
        self._update_title()
        self.statusBar().showMessage("Board rotated 90 degrees.", 3000)

    def _set_connection_mode(self, enabled: bool) -> None:
        """Update cursor and palette status for connection editing."""
        for view in (*self._views.values(), *self._side_views.values()):
            view.set_connection_mode(enabled)
        self._schematic_view.set_connection_mode(enabled)
        self._net_mode_label.setVisible(enabled)

    def _toggle_delete_mode(self) -> None:
        """Toggle continuous deletion mode from the tools palette."""
        button = getattr(self, "_delete_button", None)
        self._set_delete_mode(not button.isChecked() if button is not None else True)

    def _set_delete_mode(self, enabled: bool) -> None:
        """Enable or disable deletion mode on every board view."""
        if enabled:
            if self._pending_pad is not None:
                self._cancel_pad_placement()
            if self._pending_device is not None:
                self._cancel_device_placement()
        for view in (*self._views.values(), *self._side_views.values()):
            view.set_delete_mode(enabled)
        button = getattr(self, "_delete_button", None)
        if button is not None:
            button.blockSignals(True)
            button.setChecked(enabled)
            button.blockSignals(False)
        if enabled:
            self.statusBar().showMessage("Deleting - Esc to stop")
        else:
            self.statusBar().clearMessage()

    def _delete_mode_changed(self, enabled: bool) -> None:
        """Synchronize palette state when Escape exits deletion mode."""
        self._set_delete_mode(enabled)

    def _delete_at(self, side: str, x: float, y: float) -> None:
        """Delete the pad or footprint under a deletion-mode click."""
        if not self.project:
            return
        pad = self._pad_at(side, x, y)
        if pad is not None:
            if pad.device_id:
                self._delete_device(pad.device_id)
            else:
                self._delete_pad(pad.pad_id)
            return
        device = self._device_at(side, x, y)
        if device is not None:
            self._delete_device(device.device_id)

    def _ruler_scale_for_view(self, view: ImageView) -> float:
        """Return calibrated pixels per millimeter for one board view."""
        if view is self._views["bottom"] or view is self._side_views["bottom"]:
            side = "bottom"
        else:
            side = "top"
        if not self.project:
            return 0.0
        image = next(
            (asset for asset in self.project.images if asset.side == side), None
        )
        pixmap = self._base_pixmap_for_asset(side)
        if image is None or pixmap.isNull():
            return 0.0
        return image.measured_pixels_per_mm(pixmap.width(), pixmap.height()) or 0.0

    def _toggle_ruler(self) -> None:
        """Arm or disarm the two-click measurement tool."""
        enabled = self._ruler_button.isChecked()
        if not enabled:
            self._disable_ruler()
            return
        views = self._active_views()
        scales = [self._ruler_scale_for_view(view) for view in views]
        if not any(scales):
            self._ruler_button.setChecked(False)
            QMessageBox.information(
                self,
                "Ruler unavailable",
                "Import and calibrate an image before using the ruler.",
            )
            return
        for view, scale in zip(views, scales):
            view.set_ruler(True, scale)
        self.statusBar().showMessage("Ruler: click two points — Esc to stop.")

    def _disable_ruler(self) -> None:
        """Stop ruler mode and remove its temporary measurement lines."""
        if hasattr(self, "_ruler_button"):
            self._ruler_button.setChecked(False)
        for view in (
            *self._views.values(),
            *self._side_views.values(),
            self._overlay_view,
        ):
            view.set_ruler(False)
        self.statusBar().clearMessage()

    def _show_ruler_measurement(self, millimeters: float) -> None:
        """Display the latest ruler result in the application status bar."""
        self.statusBar().showMessage(f"Distance: {millimeters:.2f} mm")

    def _show_pad_hover(self, pad: object) -> None:
        """Display pad, device, footprint, and net information on hover."""
        if pad is None or not isinstance(pad, Pad):
            if self._pending_pad is None and self._pending_device is None:
                self.statusBar().clearMessage()
            return
        device = (
            next(
                (
                    item
                    for item in self.project.devices
                    if item.device_id == pad.device_id
                ),
                None,
            )
            if self.project and pad.device_id
            else None
        )
        device_name = device.reference if device is not None else "—"
        footprint_name = device.footprint_name if device is not None else "—"
        net_name = pad.net or "—"
        self.statusBar().showMessage(
            f"Pad: {pad.name} | Device: {device_name} | "
            f"Footprint: {footprint_name} | Net: {net_name}"
        )

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
        """Select a footprint, collect its reference, and arm placement."""
        self._set_delete_mode(False)
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        has_measurement = False
        for image in self.project.images:
            pixmap = self._base_pixmap_for_asset(image.side)
            if (
                image.measured_pixels_per_mm(pixmap.width(), pixmap.height())
                is not None
            ):
                has_measurement = True
                break
        if not has_measurement:
            legacy_scale = any(image.pixels_per_mm for image in self.project.images)
            QMessageBox.information(
                self,
                "No calibrated image",
                (
                    "This project has legacy scale data but no saved measurement "
                    "line. Use Edit image, redraw the scale line, and enter its "
                    "real length in mm."
                    if legacy_scale
                    else "Import and calibrate a board image before adding a device."
                ),
            )
            return
        self._add_device_pending = True
        index_reader = getattr(self._footprint_cache, "pad_count_index", None)
        index_ready = index_reader is None or index_reader() is not None
        if self._footprint_cache.is_ready_and_fresh() and index_ready:
            self._kicad_revision = self._footprint_cache.current_revision()
            self._open_footprint_picker()
        else:
            self._start_kicad_cache_update(interactive=True)

    def _start_kicad_cache_update(self, interactive: bool) -> None:
        """Start one asynchronous first-run/monthly cache operation."""
        if interactive or self._pad_count_index() is None:
            self._show_kicad_progress()
        if self._kicad_cache_thread is not None:
            return
        thread = QThread(self)
        worker = KiCadCacheWorker(self._footprint_cache)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._kicad_index_progress)
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

    @Slot(int, int)
    def _kicad_index_progress(self, current: int, total: int) -> None:
        """Show progress while indexing footprint pad counts."""
        if self._kicad_progress is None:
            return
        if total > 0 and self._kicad_progress.maximum() != total:
            self._kicad_progress.setRange(0, total)
            self._kicad_progress.setLabelText("Indexing footprint pad counts…")
        self._kicad_progress.setValue(current)

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
            if self._add_device_pending:
                QMessageBox.warning(self, "KiCad cache", result.warning)
            else:
                LOGGER.warning("%s", result.warning)
        if self._add_device_pending:
            QTimer.singleShot(0, self._open_footprint_picker)

    @Slot(str)
    def _kicad_cache_failed(self, message: str) -> None:
        """Report failure when no valid footprint cache is available."""
        self._close_kicad_progress()
        LOGGER.warning("KiCad footprint cache unavailable: %s", message)
        if self._add_device_pending:
            QMessageBox.warning(self, "KiCad footprints unavailable", message)
            self._add_device_pending = False

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

    def _recent_footprint_identifiers(self) -> tuple[str, ...]:
        """Return up to five persisted footprint identifiers, newest first."""
        raw = self._settings.value(_RECENT_FOOTPRINTS_KEY, [])
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, (list, tuple)):
            values = raw
        else:
            return ()
        identifiers: list[str] = []
        for value in values:
            if isinstance(value, str) and value and value not in identifiers:
                identifiers.append(value)
        return tuple(identifiers[:_MAX_RECENT_FOOTPRINTS])

    def _pad_count_index(self) -> dict[str, int] | None:
        """Read the cached pad-count index when supported by the cache."""
        reader = getattr(self._footprint_cache, "pad_count_index", None)
        return reader() if callable(reader) else None

    def _remember_recent_footprint(self, source: FootprintReference) -> None:
        """Move one selected footprint to the front of the persistent MRU list."""
        identifiers = [
            source.identifier,
            *(
                identifier
                for identifier in self._recent_footprint_identifiers()
                if identifier != source.identifier
            ),
        ][:_MAX_RECENT_FOOTPRINTS]
        self._settings.setValue(_RECENT_FOOTPRINTS_KEY, identifiers)
        self._settings.sync()

    def _select_calibration_footprint(self, parent: QWidget) -> Footprint | None:
        """Pick and load one KiCad footprint for image calibration."""
        try:
            catalog = self._footprint_cache.catalog()
        except KiCadCacheError:
            QMessageBox.information(
                parent,
                "KiCad footprints",
                "The KiCad footprint library is still being prepared. "
                "Try again in a moment.",
            )
            return None
        dialog = FootprintPickerDialog(
            catalog,
            parent,
            recent_identifiers=self._recent_footprint_identifiers(),
            pad_counts=self._pad_count_index(),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        source = dialog.selected_reference()
        if source is None:
            return None
        try:
            footprint, _content = self._footprint_cache.load(source)
        except (KiCadCacheError, KiCadFormatError) as error:
            QMessageBox.warning(parent, "Invalid footprint", str(error))
            return None
        self._remember_recent_footprint(source)
        return footprint

    def _reference_prefix(self, source: FootprintReference) -> str:
        """Choose a remembered or conventional reference prefix."""
        family = _footprint_family_key(source.library)
        if family not in {"resistor", "capacitor"}:
            return "IC"
        default_prefix = _DEFAULT_REFERENCE_PREFIXES.get(family)
        if self.project:
            for device in reversed(self.project.devices):
                if _footprint_family_key(device.footprint_library) != family:
                    continue
                match = _NUMBERED_DEVICE_REFERENCE.fullmatch(device.reference)
                if match:
                    return match.group(1)
        remembered = self._settings.value(f"{_REFERENCE_PREFIX_KEY}/{family}")
        if isinstance(remembered, str) and re.fullmatch(r"[A-Za-z]+", remembered):
            return remembered
        return default_prefix

    def _next_device_reference(self, source: FootprintReference) -> str:
        """Return the next unused numeric reference for a footprint family."""
        prefix = self._reference_prefix(source)
        used_references: set[str] = set()
        if self.project:
            for device in self.project.devices:
                used_references.add(device.reference.casefold())
        index = 1
        reference = f"{prefix}{index}"
        while reference.casefold() in used_references:
            index += 1
            reference = f"{prefix}{index}"
        return reference

    def _remember_reference_prefix(
        self, source: FootprintReference, reference: str
    ) -> None:
        """Persist the alphabetic prefix accepted for a footprint family."""
        match = _NUMBERED_DEVICE_REFERENCE.fullmatch(reference)
        if not match:
            return
        family = _footprint_family_key(source.library)
        self._settings.setValue(f"{_REFERENCE_PREFIX_KEY}/{family}", match.group(1))
        self._settings.sync()

    def _open_footprint_picker(  # pylint: disable=too-many-return-statements
        self,
    ) -> None:
        if not self._add_device_pending:
            return
        try:
            catalog = self._footprint_cache.catalog()
        except KiCadCacheError as error:
            QMessageBox.warning(self, "KiCad footprints unavailable", str(error))
            self._add_device_pending = False
            return
        dialog = FootprintPickerDialog(
            catalog,
            self,
            recent_identifiers=self._recent_footprint_identifiers(),
            pad_counts=self._pad_count_index(),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._add_device_pending = False
            return
        source = dialog.selected_reference()
        if source is None:
            self._add_device_pending = False
            return
        try:
            footprint, content = self._footprint_cache.load(source)
        except (KiCadCacheError, KiCadFormatError) as error:
            QMessageBox.warning(self, "Invalid footprint", str(error))
            self._add_device_pending = False
            return
        revision = self._kicad_revision or self._footprint_cache.current_revision()
        if revision is None:
            QMessageBox.warning(
                self, "KiCad cache", "The footprint source revision is missing."
            )
            self._add_device_pending = False
            return
        self._remember_recent_footprint(source)
        reference = self._ask_device_reference(source)
        self._add_device_pending = False
        if reference is None:
            return
        self._begin_device_placement(reference, source, footprint, content, revision)

    def _ask_device_reference(self, source: FootprintReference) -> str | None:
        """Ask for a unique device reference after footprint selection."""
        suggestion = self._next_device_reference(source)
        while self.project is not None:
            reference, accepted = QInputDialog.getText(
                self,
                "Add Footprint",
                "Component:",
                text=suggestion,
            )
            if not accepted:
                return None
            reference = reference.strip()
            if not _DEVICE_REFERENCE.fullmatch(reference):
                QMessageBox.warning(
                    self,
                    "Invalid reference",
                    "Use 1–64 letters, digits, '.', '_', '+', or '-', starting with a letter.",
                )
                continue
            if any(
                device.reference.casefold() == reference.casefold()
                for device in self.project.devices
            ):
                QMessageBox.warning(
                    self,
                    "Duplicate reference",
                    f"Device {reference} already exists.",
                )
                continue
            self._remember_reference_prefix(source, reference)
            return reference
        return None

    def _device_placement_views(
        self, calibrated: dict[str, tuple[ImageAsset, float]]
    ) -> dict[str, ImageView]:
        """Keep an active single-side view or choose the best calibrated view."""
        current_tab = self._tabs.currentIndex()
        current_side = {0: "top", 1: "bottom"}.get(current_tab)
        if current_side in calibrated:
            self._tabs.setCurrentIndex(current_tab)
            return {current_side: self._views[current_side]}
        if len(calibrated) == 2:
            self._tabs.setCurrentIndex(2)
            return self._side_views
        side = next(iter(calibrated))
        self._tabs.setCurrentIndex(0 if side == "top" else 1)
        return {side: self._views[side]}

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
        calibrated: dict[str, tuple[ImageAsset, float]] = {}
        for image in self.project.images:
            pixmap = self._base_pixmap_for_asset(image.side)
            pixels_per_mm = image.measured_pixels_per_mm(
                pixmap.width(), pixmap.height()
            )
            if pixels_per_mm is not None:
                calibrated[image.side] = (image, pixels_per_mm)
        if not calibrated:
            return
        self._cancel_device_placement(clear_status=False)
        self._pending_device = PendingDevice(
            reference, source, footprint, content, revision
        )
        views = self._device_placement_views(calibrated)
        for side, view in views.items():
            calibration = calibrated.get(side)
            if calibration and view.has_image():
                _image, pixels_per_mm = calibration
                view.set_device_placement(footprint, pixels_per_mm, side, 0.0)
                LOGGER.info(
                    "Footprint preview scale side=%s pixels_per_mm=%.6f",
                    side,
                    pixels_per_mm,
                )
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
        """Persist one footprint and immediately arm the next reference."""
        pending = self._pending_device
        if not self.project or not self.store or pending is None:
            return
        view_context = (
            self._tabs.currentIndex(),
            self._active_views()[0].view_state(),
        )
        image = next(
            (asset for asset in self.project.images if asset.side == side), None
        )
        pixmap = self._base_pixmap_for_asset(side)
        pixels_per_mm = (
            image.measured_pixels_per_mm(pixmap.width(), pixmap.height())
            if image is not None and not pixmap.isNull()
            else None
        )
        if image is None or pixels_per_mm is None or pixmap.isNull():
            QMessageBox.warning(
                self,
                "Cannot place device",
                "The selected image needs a saved scale line and real length.",
            )
            return
        try:
            if any(
                existing.reference.casefold() == pending.reference.casefold()
                for existing in self.project.devices
            ):
                raise ProjectFormatError(f"Device {pending.reference} already exists.")
            placed_pads = place_footprint_pads(
                pending.footprint,
                side,
                x,
                y,
                pending.rotation,
                pixmap.width(),
                pixmap.height(),
                pixels_per_mm,
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
                object_type=_footprint_family(pending.footprint.library),
                pins=[
                    ComponentPin(
                        number=placed.number,
                        pin_id=placed.number,
                        footprint_pad=placed.number,
                    )
                    for placed in placed_pads
                ],
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
        self._pending_device = replace(
            pending, reference=self._next_device_reference(pending.source)
        )
        self._dirty = True
        LOGGER.info(
            "Device placed id=%s reference=%s side=%s rotation=%s pads=%s next=%s",
            device.device_id,
            device.reference,
            side,
            device.rotation,
            len(generated),
            self._pending_device.reference,
        )
        self._refresh_views()
        self._tabs.setCurrentIndex(view_context[0])
        self._apply_active_view_state(view_context[1])
        self.statusBar().showMessage(
            f"Place {self._pending_device.reference}: left click to place, "
            "right click rotates 45°, Esc ends the series."
        )
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
        self._set_delete_mode(False)
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
        self.statusBar().showMessage("Adding Pad - Esc to stop")

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
        self._pending_pad = Pad(self._next_pad_name(), "top", 0.0, 0.0)
        self._dirty = True
        self.statusBar().showMessage("Adding Pad - Esc to stop")
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

    def _device_at(  # pylint: disable=too-many-locals
        self, side: str, x: float, y: float
    ) -> Device | None:
        """Return a footprint whose physical drawing contains the point."""
        if not self.project:
            return None
        pixmap = self._base_pixmap_for_asset(side)
        if pixmap.isNull():
            return None
        image = next(
            (asset for asset in self.project.images if asset.side == side), None
        )
        if image is None:
            return None
        pixels_per_mm = image.measured_pixels_per_mm(pixmap.width(), pixmap.height())
        if pixels_per_mm is None:
            pixels_per_mm = image.pixels_per_mm
        if pixels_per_mm is None:
            return None
        image_width = max(1, pixmap.width() - 1)
        image_height = max(1, pixmap.height() - 1)
        for device in reversed(self.project.devices):
            if device.side != side:
                continue
            footprint = self._footprint_for_device(device)
            if footprint is None:
                continue
            radius = max(6.0, footprint.radius() * pixels_per_mm)
            delta_x = (x - device.x) * image_width
            delta_y = (y - device.y) * image_height
            if math.hypot(delta_x, delta_y) <= radius:
                return device
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
        if self._pending_board_connection_pads:
            self._select_board_connection_pad(side, x, y)
            return
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

    def _select_board_connection_pad(self, side: str, x: float, y: float) -> None:
        """Connect each new Shift-clicked pad to the session origin immediately."""
        pad = self._pad_at(side, x, y)
        if pad is None:
            return
        self._set_connection_mode(True)
        view_state = self._active_views()[0].view_state()
        if not self._pending_board_connection_pads:
            selected = next(
                (
                    item
                    for item in self.project.pads
                    if item.pad_id == self._selected_pad_id
                ),
                None,
            )
            if selected is not None:
                self._pending_board_connection_pads.append(selected.pad_id)
            else:
                self._pending_board_connection_pads.append(pad.pad_id)
                self._selected_pad_id = pad.pad_id
                self._selected_net = pad.net
                self.statusBar().showMessage(
                    "Connector mode: origin selected; Shift-click another pad."
                )
                self._schedule_pad_refresh(view_state)
                return
        if pad.pad_id in self._pending_board_connection_pads:
            return
        origin = next(
            (
                item
                for item in self.project.pads
                if item.pad_id == self._pending_board_connection_pads[0]
            ),
            None,
        )
        if origin is None:
            self._pending_board_connection_pads = []
            return
        self._pending_board_connection_pads.append(pad.pad_id)
        self._selected_pad_id = origin.pad_id
        self._set_connection_mode(True)
        self._connect_schematic_terminals(
            self._pad_terminal(origin), self._pad_terminal(pad)
        )
        self._apply_active_view_state(view_state)
        count = len(self._pending_board_connection_pads)
        self.statusBar().showMessage(
            f"Connector mode: {count} pad(s) linked; Esc finishes."
        )

    @staticmethod
    def _pad_terminal(pad: Pad) -> tuple[str, str, str | None]:
        """Convert a physical pad into its schematic terminal identity."""
        if pad.device_id and pad.number:
            return "pin", pad.device_id, pad.number
        return "pad", pad.pad_id, None

    def _finish_board_connection(self) -> bool:
        """Leave board connector mode; links were created after every click."""
        if not self._pending_board_connection_pads:
            return False
        self._pending_board_connection_pads = []
        self._set_connection_mode(False)
        self.statusBar().showMessage("Connector mode finished.", 3000)
        return True

    def _set_pads_visible(self, visible: bool) -> None:
        """Show or hide pad overlays while preserving the current view."""
        self._pads_visible = visible
        self._pad_display_mode = "both" if visible else "image"
        LOGGER.info("Pad visibility=%s", visible)
        self._schedule_pad_refresh(self._active_views()[0].view_state())

    def _cycle_pad_display_mode(self) -> None:
        """Cycle between pad+image, image-only, and pad-only display."""
        modes = ("both", "image", "pads")
        self._pad_display_mode = modes[(modes.index(self._pad_display_mode) + 1) % 3]
        self._pads_visible = self._pad_display_mode != "image"
        labels = {
            "both": "Pad + image",
            "image": "Image only",
            "pads": "Pad only",
        }
        label = labels[self._pad_display_mode]
        self._show_pads_button.setToolTip(label)
        self._show_pads_button.setStatusTip(label)
        self.statusBar().showMessage(f"Display: {label}", 2000)
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
        current_tab = self._tabs.currentIndex()
        self._pending_pad_view_state = None
        self._pad_refresh_pending = False
        LOGGER.debug("Pad refresh started state=%s", state)
        self._refresh_views()
        self._tabs.setCurrentIndex(current_tab)
        if state is not None:
            self._apply_active_view_state(state)

    def _refresh_views_preserving_state(self) -> None:
        """Refresh overlays without changing the current tab, zoom, or pan."""
        current_tab = self._tabs.currentIndex()
        state = self._active_views()[0].view_state()
        self._refresh_views()
        self._tabs.setCurrentIndex(current_tab)
        self._apply_active_view_state(state)

    def _apply_active_view_state(self, state: tuple[float, float, float]) -> None:
        """Restore zoom and pan after refreshing visible pad markers."""
        self._syncing_views = True
        try:
            for view in self._active_views():
                view.apply_view_state(state)
        finally:
            self._syncing_views = False
        if self._tabs.currentIndex() not in (3, 4, 5, 6):
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
        active_net = self._selected_net
        active_pad_id = self._selected_pad_id
        net = value.strip() or None
        self.project.pads = [
            replace(item, net=net) if item.pad_id == pad_id else item
            for item in self.project.pads
        ]
        changed_pad = next(
            (item for item in self.project.pads if item.pad_id == pad_id), None
        )
        if changed_pad is not None and changed_pad.device_id and changed_pad.number:
            self.project.devices = [
                (
                    replace(
                        device,
                        pins=[
                            (
                                replace(pin, net_id=net)
                                if pin.number == changed_pad.number
                                else pin
                            )
                            for pin in device.pins
                        ],
                    )
                    if device.device_id == changed_pad.device_id
                    else device
                )
                for device in self.project.devices
            ]
        keep_active_selection = (
            net is None
            and active_net == pad.net
            and active_pad_id is not None
            and active_pad_id != pad_id
        )
        if keep_active_selection:
            self._selected_net = active_net
            self._selected_pad_id = active_pad_id
        else:
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
        """Open the pad/device action menu after returning from the mouse event."""
        QTimer.singleShot(0, lambda: self._show_pad_menu(side, x, y))

    def _defer_trace_menu(self, side: str, x: float, y: float) -> None:
        """Open the trace action menu after returning from the mouse event."""
        QTimer.singleShot(0, lambda: self._show_trace_menu(side, x, y))

    def _show_pad_menu(self, side: str, x: float, y: float) -> None:
        """Show non-blocking actions for the right-clicked pad or device."""
        pad = self._pad_at(side, x, y)
        device = (
            next(
                (
                    item
                    for item in self.project.devices
                    if item.device_id == pad.device_id
                ),
                None,
            )
            if self.project and pad is not None and pad.device_id
            else self._device_at(side, x, y)
        )
        if pad is None and device is None:
            return
        if self._pad_menu is not None:
            self._pad_menu.close()
        menu = QMenu(self)
        if device is None:
            delete_action = menu.addAction("Delete pad")
            delete_action.triggered.connect(lambda: self._delete_pad(pad.pad_id))
        else:
            delete_action = menu.addAction("Delete device")
            component_action = menu.addAction("Set Component…")
            value_action = menu.addAction("Set Value…")
            description_action = menu.addAction("Edit description…")
            datasheet_action = menu.addAction("Edit datasheet…")
            delete_action.triggered.connect(
                lambda: self._delete_device(device.device_id)
            )
            value_action.triggered.connect(
                lambda: self._edit_device_value(device.device_id)
            )
            component_action.triggered.connect(
                lambda: self._edit_device_component(device.device_id)
            )
            description_action.triggered.connect(
                lambda: self._edit_device_metadata(
                    device.device_id, "description", "Description"
                )
            )
            datasheet_action.triggered.connect(
                lambda: self._edit_device_metadata(
                    device.device_id, "datasheet", "Datasheet URL"
                )
            )
            if pad is not None and pad.number is not None:
                pin_action = menu.addAction("Edit function…")
                pin_action.triggered.connect(
                    lambda: self._edit_component_pin(device.device_id, pad.number)
                )
        if pad is not None:
            connect_action = menu.addAction("Connect to net")
            disconnect_action = menu.addAction("Disconnect")
            disconnect_action.setEnabled(pad.net is not None)
            connect_action.triggered.connect(
                lambda: self._connect_pad_to_net(side, x, y)
            )
            disconnect_action.triggered.connect(
                lambda: self._assign_pad_net(pad.pad_id, "")
            )
        menu.aboutToHide.connect(lambda menu=menu: self._pad_menu_closed(menu))
        self._pad_menu = menu
        LOGGER.info(
            "Selection menu opened pad=%s device=%s",
            pad.pad_id if pad else None,
            device.device_id if device else None,
        )
        menu.popup(QCursor.pos())

    def _show_trace_menu(self, side: str, x: float, y: float) -> None:
        """Show Disconnect for the selected trace under the cursor."""
        view = self._views.get(side) or self._side_views.get(side)
        if view is None:
            return
        trace = view.trace_at(x, y)
        if trace is None:
            return
        if self._pad_menu is not None:
            self._pad_menu.close()
        menu = QMenu(self)
        disconnect_action = menu.addAction("Disconnect")
        disconnect_action.triggered.connect(
            lambda _checked=False, second=trace[1]: self._disconnect_trace(second)
        )
        menu.aboutToHide.connect(lambda menu=menu: self._pad_menu_closed(menu))
        self._pad_menu = menu
        menu.popup(QCursor.pos())

    def _disconnect_trace(self, pad_id: str) -> None:
        """Remove one trace endpoint while preserving remaining NET links."""
        if not self.project or not self._selected_net:
            return
        target = next((pad for pad in self.project.pads if pad.pad_id == pad_id), None)
        if target is None or target.net != self._selected_net:
            return
        net = self._selected_net
        self.project.pads = [
            replace(pad, net=None) if pad.pad_id == pad_id else pad
            for pad in self.project.pads
        ]
        if target.device_id and target.number:
            self.project.devices = [
                (
                    replace(
                        device,
                        pins=[
                            (
                                replace(pin, net_id=None)
                                if pin.number == target.number
                                else pin
                            )
                            for pin in device.pins
                        ],
                    )
                    if device.device_id == target.device_id
                    else device
                )
                for device in self.project.devices
            ]
        remaining = [pad for pad in self.project.pads if pad.net == net]
        self._selected_net = net if remaining else None
        self._dirty = True
        self._schedule_pad_refresh(self._active_views()[0].view_state())
        self._schematic_view.set_selected_net(self._selected_net)
        self._update_title()
        self.statusBar().showMessage("Trace disconnected.", 3000)

    def _edit_component_pin(self, device_id: str, number: str) -> None:
        """Edit the function for one KiCad component pin."""
        if not self.project:
            return
        device = next(
            (item for item in self.project.devices if item.device_id == device_id),
            None,
        )
        if device is None:
            return
        pins = list(device.pins)
        pin_index = next(
            (index for index, pin in enumerate(pins) if pin.number == number), None
        )
        if pin_index is None:
            pins.append(ComponentPin(number, number, footprint_pad=number))
            pin_index = len(pins) - 1
        pin = pins[pin_index]
        function, accepted = QInputDialog.getText(
            self,
            "Pin function",
            f"Function for {device.reference}.{number}:",
            text=pin.function,
        )
        if not accepted:
            return
        pins[pin_index] = replace(
            pin,
            function=function.strip(),
        )
        self.project.devices = [
            replace(item, pins=pins) if item.device_id == device_id else item
            for item in self.project.devices
        ]
        self._dirty = True
        self._update_title()

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
        view_state = self._active_views()[0].view_state()
        current_tab = self._tabs.currentIndex()
        self.project.pads = [
            item for item in self.project.pads if item.pad_id != pad_id
        ]
        if self._selected_pad_id == pad_id:
            self._selected_pad_id = None
            self._selected_net = None
        self._dirty = True
        LOGGER.info("Pad deleted id=%s pad=%s", pad.pad_id, pad.name)
        self._refresh_views()
        self._tabs.setCurrentIndex(current_tab)
        self._apply_active_view_state(view_state)
        self._update_title()

    def _edit_device_value(self, device_id: str) -> None:
        """Open a non-modal value editor for one placed KiCad device."""
        if not self.project:
            return
        device = next(
            (item for item in self.project.devices if item.device_id == device_id),
            None,
        )
        if device is None:
            return
        if self._device_value_dialog is not None:
            self._device_value_dialog.close()
        dialog = QInputDialog(self)
        dialog.setWindowTitle("Component value")
        dialog.setLabelText(f"Value for Component {device.reference}:")
        dialog.setTextValue(device.value)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.accepted.connect(
            lambda device_id=device_id, dialog=dialog: self._assign_device_value(
                device_id, dialog.textValue()
            )
        )
        dialog.finished.connect(
            lambda result, dialog=dialog: self._device_value_dialog_finished(
                dialog, result
            )
        )
        self._device_value_dialog = dialog
        LOGGER.info("Device value dialog opened id=%s", device_id)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _edit_device_component(self, device_id: str) -> None:
        """Open the component ID editor from the footprint context menu."""
        if not self.project:
            return
        device = next(
            (item for item in self.project.devices if item.device_id == device_id),
            None,
        )
        if device is None:
            return
        reference, accepted = QInputDialog.getText(
            self,
            "Set component",
            f"Component ID for {device.reference}:",
            text=device.reference,
        )
        if accepted:
            self._rename_device(device_id, reference)

    def _assign_device_value(self, device_id: str, value: str) -> None:
        """Persist a BOM value entered for one device."""
        if not self.project:
            return
        device = next(
            (item for item in self.project.devices if item.device_id == device_id),
            None,
        )
        if device is None:
            return
        value = value.strip()
        self.project.devices = [
            replace(item, value=value) if item.device_id == device_id else item
            for item in self.project.devices
        ]
        self._dirty = True
        self._refresh_bom_table()
        self._update_title()
        LOGGER.info(
            "Device value assigned id=%s reference=%s value=%s",
            device_id,
            device.reference,
            value,
        )

    def _device_value_dialog_finished(self, dialog: QInputDialog, _result: int) -> None:
        """Release the asynchronous device-value dialog reference."""
        if self._device_value_dialog is dialog:
            self._device_value_dialog = None

    def _delete_device(self, device_id: str) -> None:
        """Delete one footprint instance, all generated pads, and its asset."""
        if not self.project:
            return
        device = next(
            (item for item in self.project.devices if item.device_id == device_id),
            None,
        )
        if device is None:
            return
        current_tab = self._tabs.currentIndex()
        view_state = self._active_views()[0].view_state()
        removed_pad_ids = {
            pad.pad_id for pad in self.project.pads if pad.device_id == device_id
        }
        self.project.devices = [
            item for item in self.project.devices if item.device_id != device_id
        ]
        self.project.pads = [
            pad for pad in self.project.pads if pad.device_id != device_id
        ]
        if self.store is not None:
            self.store.remove_asset(device.footprint_path)
        self._device_footprint_cache.pop(device.footprint_path, None)
        if self._selected_pad_id in removed_pad_ids:
            self._selected_pad_id = None
            self._selected_net = None
        self._dirty = True
        LOGGER.info(
            "Device deleted id=%s reference=%s pads=%s",
            device.device_id,
            device.reference,
            len(removed_pad_ids),
        )
        self._refresh_views()
        self._tabs.setCurrentIndex(current_tab)
        self._apply_active_view_state(view_state)
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
        self._import_action = QAction("Manage image", self)
        self._import_action.setShortcut("I")
        self._import_action.setToolTip("Load, resize, or remove a Top or Bottom image")
        self._import_action.triggered.connect(self.manage_picture)
        file_menu.addAction(self._import_action)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """Switch board view with `T`, `B`, or a double press."""
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if (
                getattr(self, "_ruler_button", None) is not None
                and self._ruler_button.isChecked()
            ):
                self._disable_ruler()
                event.accept()
                return
            if self._pending_pad is not None:
                self._cancel_pad_placement()
                event.accept()
                return
            connected = self._finish_board_connection()
            connected = self._schematic_view.finish_connection() or connected
            if connected:
                event.accept()
                return
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

    def _cancel_pad_placement(self) -> None:
        """Stop continuous pad placement without changing saved pads."""
        self._pending_pad = None
        for view in (*self._views.values(), *self._side_views.values()):
            view.set_pad_placement(False)
        self.statusBar().clearMessage()

    def new_project(self) -> None:
        """Create an empty `.revp` project file."""
        if not self._confirm_pending_changes():
            return
        dialog = ProjectDetailsDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Create project",
            str(self._last_project_directory()),
            "Tnasrevner project (*.revp)",
        )
        if not path:
            return
        project_path = Path(path)
        if project_path.suffix.lower() != ".revp":
            project_path = project_path.with_suffix(".revp")
        self._remember_project_directory(project_path)
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
            self,
            "Open project",
            str(self._last_project_directory()),
            "Tnasrevner project (*.revp)",
        )
        if not path:
            return
        self._remember_project_directory(Path(path))
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

    def _last_project_directory(self) -> Path:
        """Return remembered project directory, falling back if it vanished."""
        value = self._settings.value(_LAST_PROJECT_DIRECTORY_KEY, "")
        if isinstance(value, str):
            directory = Path(value).expanduser()
            if directory.is_dir():
                return directory
        home = Path.home()
        return home if home.is_dir() else Path.cwd()

    def _remember_project_directory(self, project_path: Path) -> None:
        """Persist project parent directory for the next file dialog."""
        directory = project_path.expanduser().parent
        if not directory.is_dir():
            return
        self._settings.setValue(_LAST_PROJECT_DIRECTORY_KEY, str(directory))
        self._settings.sync()

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
            "bom",
            "schematic",
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
            self._add_device_pending = False
            self._close_kicad_progress()
            self._close_when_cache_finishes = True
            self.hide()
            event.ignore()
            return
        event.accept()

    def manage_picture(self) -> None:
        """Choose a side, then load it or open its image editor."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        side = self._choose_image_side()
        if side is None:
            return
        if any(image.side == side for image in self.project.images):
            self.edit_picture(side)
        else:
            self.import_picture(side)

    def import_picture(self, side: str | None = None) -> None:
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
        side = side or self._choose_image_side()
        if side is None:
            return
        edited = self._edit_imported_image(QPixmap(str(source_path)), side=side)
        if edited is None:
            return
        edited_image, pixels_per_mm, calibration_line, calibration_length_mm = edited
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
                calibration_length_mm,
            )
        )
        self._image_cache.pop(side, None)
        self._rebuild_device_pads(side, edited_image)
        self._dirty = True
        self._refresh_views()
        self._update_title()

    def remove_picture(self, side: str | None = None) -> None:
        """Remove selected Top or Bottom image after explicit side choice."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        side = side or self._choose_image_side()
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

    def edit_picture(self, side: str | None = None) -> None:
        """Reopen the working image for crop and rotation adjustments."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        side = side or self._choose_image_side()
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
            image,
            asset.pixels_per_mm,
            asset.calibration_line,
            asset.calibration_length_mm,
            side=side,
        )
        if edited is None:
            return
        edited_image, pixels_per_mm, calibration_line, calibration_length_mm = edited
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
                    calibration_length_mm,
                )
                if image.side == side
                else image
            )
            for image in self.project.images
        ]
        self._image_cache.pop(side, None)
        self._rebuild_device_pads(side, edited_image)
        self._dirty = True
        self._refresh_views()
        self._update_title()

    def _edit_imported_image(
        self,
        image: QPixmap,
        pixels_per_mm: float | None = None,
        calibration_line: tuple[float, float, float, float] | None = None,
        calibration_length_mm: float | None = None,
        side: str | None = None,
    ) -> (
        tuple[
            QPixmap,
            float,
            tuple[float, float, float, float] | None,
            float,
        ]
        | None
    ):
        """Open editor and return image plus scale, or `None` on cancel."""
        dialog = ImageEditDialog(
            image,
            self,
            footprint_selector=self._select_calibration_footprint,
            load_callback=(lambda: self.import_picture(side)) if side else None,
            remove_callback=(lambda: self.remove_picture(side)) if side else None,
        )
        if pixels_per_mm is not None:
            dialog.prepare_existing_image(
                pixels_per_mm, calibration_line, calibration_length_mm
            )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return (
            dialog.result_pixmap(),
            dialog.pixels_per_mm(),
            dialog.calibration_line(),
            dialog.calibration_length_mm(),
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
            view.set_trace_selection(self._selected_net, self._selected_pad_id)
            view.set_footprint_overlays(self._vector_footprints_for_side(side))
            view.set_pad_labels(self._vector_labels_for_side(side))
            view.set_pixmap(images[side])
        self._refresh_overlay(images)
        for side, view in self._side_views.items():
            view.set_trace_selection(self._selected_net, self._selected_pad_id)
            view.set_footprint_overlays(self._vector_footprints_for_side(side))
            view.set_pad_labels(self._vector_labels_for_side(side))
            view.set_pixmap(images[side])
        overlay_labels = self._vector_labels_for_side("top") + tuple(
            self._vector_labels_for_side("bottom")
        )
        self._overlay_view.set_trace_selection(
            self._selected_net, self._selected_pad_id
        )
        self._overlay_view.set_footprint_overlays(())
        self._overlay_view.set_pad_labels(overlay_labels)
        self._refresh_net_table()
        self._refresh_bom_table()
        self._schematic_view.set_project(self.project)
        if self.project:
            self._tabs.setCurrentIndex(
                {
                    "top": 0,
                    "bottom": 1,
                    "side_by_side": 2,
                    "both": 3,
                    "nets": 4,
                    "bom": 5,
                    "schematic": 6,
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
            if self._tabs.currentIndex() not in (3, 4, 5, 6):
                self._sync_board_views(self._active_views()[0])
        else:
            self._sync_board_views(self._views["top"])
        LOGGER.debug("View refresh completed")

    def _vector_labels_for_side(self, side: str) -> tuple[Pad, ...]:
        """Return visible pad labels for vector rendering in one view."""
        if self._pad_display_mode == "image" or not self.project:
            return ()
        return tuple(pad for pad in self.project.pads if pad.side == side)

    def _vector_footprints_for_side(self, side: str) -> tuple[tuple, ...]:
        """Return placed footprints for vector rendering in one view."""
        if self._pad_display_mode != "both" or not self.project:
            return ()
        image = next(
            (asset for asset in self.project.images if asset.side == side), None
        )
        pixmap = self._base_pixmap_for_asset(side)
        if image is None or pixmap.isNull():
            return ()
        pixels_per_mm = image.measured_pixels_per_mm(pixmap.width(), pixmap.height())
        if pixels_per_mm is None:
            pixels_per_mm = image.pixels_per_mm
        if pixels_per_mm is None:
            return ()
        overlays = []
        for device in self.project.devices:
            if device.side != side:
                continue
            footprint = self._footprint_for_device(device)
            if footprint is not None:
                overlays.append(
                    (
                        footprint,
                        device.x,
                        device.y,
                        device.rotation,
                        pixels_per_mm,
                        side,
                        pixmap.width(),
                    )
                )
        return tuple(overlays)

    def _refresh_net_table(self) -> None:
        """Show pads, logical pins, functions, and assigned nets."""
        self._ensure_component_pins()
        pads = (
            sorted(self.project.pads, key=lambda pad: pad.name) if self.project else []
        )
        self._net_table.blockSignals(True)
        try:
            self._net_table.setRowCount(len(pads))
            for row, pad in enumerate(pads):
                pad_item = QTableWidgetItem(pad.name)
                pad_item.setFlags(pad_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                pad_item.setData(Qt.ItemDataRole.UserRole, pad.pad_id)
                self._net_table.setItem(row, 0, pad_item)
                pin, device = self._component_pin_for_pad(pad)
                pin_text = pin.number if pin is not None else ""
                function_text = (
                    pin.function
                    if pin is not None and pin.function
                    else (
                        self._default_pin_function(device, pin)
                        if pin is not None
                        else pad.function
                    )
                )
                for column, value in (
                    (1, pad.net or ""),
                    (2, pin_text),
                    (3, function_text),
                    (4, device.reference if device is not None else ""),
                    (5, device.value if device is not None else ""),
                ):
                    item = QTableWidgetItem(value)
                    if column in (2, 4, 5):
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    item.setData(
                        Qt.ItemDataRole.UserRole,
                        (
                            (pad.pad_id, device.device_id, pin.number)
                            if pin is not None and device is not None
                            else (pad.pad_id, None, pad.number)
                        ),
                    )
                    self._net_table.setItem(row, column, item)
        finally:
            self._net_table.blockSignals(False)

    def _ensure_component_pins(self) -> None:
        """Migrate loaded footprint pads into complete component pin records."""
        if not self.project:
            return
        pads_by_device: dict[str, list[Pad]] = {}
        for pad in self.project.pads:
            if pad.device_id and pad.number:
                pads_by_device.setdefault(pad.device_id, []).append(pad)
        devices: list[Device] = []
        changed = False
        for device in self.project.devices:
            pins = list(device.pins)
            for pad in pads_by_device.get(device.device_id, []):
                pin_index = next(
                    (
                        index
                        for index, pin in enumerate(pins)
                        if pin.number == pad.number
                    ),
                    None,
                )
                if pin_index is None:
                    pin = ComponentPin(
                        pad.number,
                        pad.number,
                        footprint_pad=pad.number,
                    )
                    function = self._default_pin_function(device, pin)
                    pins.append(replace(pin, function=function) if function else pin)
                    changed = True
                    continue
                pin = pins[pin_index]
                if not pin.function:
                    function = self._default_pin_function(device, pin)
                    if function:
                        pins[pin_index] = replace(pin, function=function)
                        changed = True
            devices.append(
                replace(device, pins=pins) if pins != device.pins else device
            )
        if changed:
            self.project.devices = devices
            self._dirty = True

    def _component_pin_for_pad(
        self, pad: Pad
    ) -> tuple[ComponentPin | None, Device | None]:
        """Find the logical pin and owning component for one physical pad."""
        if not self.project or not pad.device_id or not pad.number:
            return None, None
        device = next(
            (item for item in self.project.devices if item.device_id == pad.device_id),
            None,
        )
        if device is None:
            return None, None
        pin = next((item for item in device.pins if item.number == pad.number), None)
        return pin, device

    @staticmethod
    def _default_pin_function(device: Device | None, pin: ComponentPin | None) -> str:
        """Return a useful electrical label when a pin function is empty."""
        if device is None or pin is None:
            return ""
        family = _footprint_family(device.footprint_library).casefold()
        footprint = device.footprint_name.casefold()
        if "capacitor" in family or "capacitor" in footprint:
            if any(word in footprint for word in ("electro", "polar", "cp")):
                return "+" if pin.number in {"1", "+"} else "-"
            return "CNX"
        return pin.net_id or f"Pin {pin.number}"

    def _net_table_cell_changed(self, row: int, column: int) -> None:
        """Persist function or net edits made in the Nets tab."""
        if not self.project or column not in (1, 3):
            return
        pad_item = self._net_table.item(row, 0)
        value_item = self._net_table.item(row, column)
        if pad_item is None or value_item is None:
            return
        pad_id = pad_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(pad_id, str):
            return
        value = value_item.text().strip()
        pad = next((item for item in self.project.pads if item.pad_id == pad_id), None)
        _, device = self._component_pin_for_pad(pad) if pad else (None, None)
        if column == 1:
            self._assign_pad_net(pad_id, value)
            return
        if pad is None:
            return
        if device is None:
            self.project.pads = [
                replace(item, function=value) if item.pad_id == pad_id else item
                for item in self.project.pads
            ]
            self._dirty = True
            self._schematic_view.set_project(self.project)
            self._update_title()
            return
        if not pad.number:
            return
        pins = list(device.pins)
        pin_index = next(
            (index for index, item in enumerate(pins) if item.number == pad.number),
            None,
        )
        if pin_index is None:
            pins.append(ComponentPin(pad.number, pad.number, footprint_pad=pad.number))
            pin_index = len(pins) - 1
        current = pins[pin_index]
        pins[pin_index] = replace(
            current,
            function=value,
        )
        self.project.devices = [
            replace(item, pins=pins) if item.device_id == device.device_id else item
            for item in self.project.devices
        ]
        self._dirty = True
        current_tab = self._tabs.currentIndex()
        self._refresh_views()
        self._tabs.setCurrentIndex(current_tab)
        self._update_title()

    def _refresh_bom_table(self) -> None:
        """Show every placed KiCad device and its BOM metadata."""
        devices = (
            sorted(
                self.project.devices,
                key=lambda device: _reference_sort_key(device.reference),
            )
            if self.project
            else []
        )
        object_types = sorted(
            {
                device.object_type or _footprint_family(device.footprint_library)
                for device in devices
            },
            key=str.casefold,
        )
        self._bom_table.blockSignals(True)
        try:
            self._bom_table.setRowCount(len(devices))
            for row, device in enumerate(devices):
                reference_item = QTableWidgetItem(device.reference)
                reference_item.setData(Qt.ItemDataRole.UserRole, device.device_id)
                self._bom_table.setItem(row, 0, reference_item)
                combo = QComboBox()
                combo.addItems(object_types)
                combo.addItem("NEW")
                combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
                combo.setMinimumContentsLength(
                    max((len(item) for item in (*object_types, "NEW")), default=8)
                )
                combo.setMinimumWidth(180)
                combo.view().setMinimumWidth(max(220, combo.sizeHint().width()))
                current_type = device.object_type or _footprint_family(
                    device.footprint_library
                )
                object_item = QTableWidgetItem(current_type)
                object_item.setFlags(object_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._bom_table.setItem(row, 1, object_item)
                combo.setCurrentText(current_type)
                combo.currentTextChanged.connect(
                    lambda text, device_id=device.device_id, combo=combo: self._bom_object_changed(
                        device_id, combo, text
                    )
                )
                self._bom_table.setCellWidget(row, 1, combo)
                footprint_item = QTableWidgetItem(device.footprint_name)
                footprint_item.setFlags(
                    footprint_item.flags() & ~Qt.ItemFlag.ItemIsEditable
                )
                self._bom_table.setItem(row, 2, footprint_item)
                value_item = QTableWidgetItem(device.value)
                self._bom_table.setItem(row, 3, value_item)
                description_item = QTableWidgetItem(device.description)
                description_item.setFlags(
                    description_item.flags() & ~Qt.ItemFlag.ItemIsEditable
                )
                self._bom_table.setItem(row, 4, description_item)
                datasheet_item = QTableWidgetItem(device.datasheet)
                self._bom_table.setItem(row, 5, datasheet_item)
        finally:
            self._bom_table.blockSignals(False)
        self._bom_table.resizeColumnsToContents()
        header = self._bom_table.horizontalHeader()
        self._bom_table.setColumnWidth(1, max(180, self._bom_table.columnWidth(1)))
        for column in range(self._bom_table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

    def _bom_table_cell_changed(self, row: int, column: int) -> None:
        """Persist edits from the BOM Value or Datasheet columns."""
        if not self.project or column not in (0, 3, 5):
            return
        reference_item = self._bom_table.item(row, 0)
        value_item = self._bom_table.item(row, column)
        if reference_item is None or value_item is None:
            return
        device_id = reference_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(device_id, str):
            return
        if column == 0:
            self._rename_device(device_id, value_item.text())
            return
        field = "value" if column == 3 else "datasheet"
        self.project.devices = [
            (
                replace(device, **{field: value_item.text()})
                if device.device_id == device_id
                else device
            )
            for device in self.project.devices
        ]
        self._dirty = True
        self._schematic_view.set_project(self.project)
        self._update_title()

    def _rename_device(self, device_id: str, reference: str) -> None:
        """Rename a device while preserving its identity and connections."""
        if not self.project:
            return
        device = next(
            (item for item in self.project.devices if item.device_id == device_id),
            None,
        )
        if device is None:
            return
        reference = reference.strip()
        duplicate = any(
            item.device_id != device_id
            and item.reference.casefold() == reference.casefold()
            for item in self.project.devices
        )
        if not _DEVICE_REFERENCE.fullmatch(reference) or duplicate:
            QMessageBox.warning(
                self,
                "Invalid reference",
                "The reference must be unique and contain only letters, numbers, "
                "or _ + - . characters.",
            )
            self._refresh_bom_table()
            return
        self.project.devices = [
            replace(item, reference=reference) if item.device_id == device_id else item
            for item in self.project.devices
        ]
        old_prefix = f"{device.reference}."
        new_prefix = f"{reference}."
        self.project.pads = [
            replace(
                pad,
                name=(
                    new_prefix + pad.name[len(old_prefix) :]
                    if pad.device_id == device_id and pad.name.startswith(old_prefix)
                    else pad.name
                ),
            )
            for pad in self.project.pads
        ]
        self._dirty = True
        self._refresh_views_preserving_state()
        self._update_title()

    def _show_bom_menu(self, position: QPoint) -> None:
        """Offer multiline note and description editing for a BOM device."""
        index = self._bom_table.indexAt(position)
        if not index.isValid():
            return
        reference_item = self._bom_table.item(index.row(), 0)
        if reference_item is None:
            return
        device_id = reference_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(device_id, str) or not self.project:
            return
        device = next(
            (item for item in self.project.devices if item.device_id == device_id),
            None,
        )
        if device is None:
            return
        menu = QMenu(self)
        description_action = menu.addAction("Edit description…")
        note_action = menu.addAction("Edit note…")
        action = menu.exec(self._bom_table.viewport().mapToGlobal(position))
        if action == description_action:
            description, accepted = QInputDialog.getText(
                self,
                "Device description",
                f"Description for {device.reference}:",
                text=device.description,
            )
            if accepted:
                self._set_device_text(device_id, "description", description)
        elif action == note_action:
            self._edit_device_note(device)

    def _edit_device_note(self, device: Device) -> None:
        """Edit a device note in a larger multiline dialog."""
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Note for {device.reference}")
        layout = QVBoxLayout(dialog)
        editor = QPlainTextEdit()
        editor.setPlainText(device.note)
        editor.setMinimumSize(520, 260)
        layout.addWidget(editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._set_device_text(device.device_id, "note", editor.toPlainText())

    def _set_device_text(self, device_id: str, field: str, value: str) -> None:
        """Persist a text metadata field and refresh the BOM."""
        if not self.project or field not in {"description", "note", "datasheet"}:
            return
        self.project.devices = [
            (
                replace(device, **{field: value})
                if device.device_id == device_id
                else device
            )
            for device in self.project.devices
        ]
        self._dirty = True
        self._refresh_bom_table()
        self._schematic_view.set_project(self.project)
        self._update_title()

    def _edit_device_metadata(self, device_id: str, field: str, label: str) -> None:
        """Edit one short device metadata field from the footprint menu."""
        if not self.project or field not in {"description", "datasheet"}:
            return
        device = next(
            (item for item in self.project.devices if item.device_id == device_id),
            None,
        )
        if device is None:
            return
        value, accepted = QInputDialog.getText(
            self,
            label,
            f"{label} for {device.reference}:",
            text=getattr(device, field),
        )
        if accepted:
            self._set_device_text(device_id, field, value)

    def _bom_object_changed(
        self, device_id: str, combo: QComboBox, selected: str
    ) -> None:
        """Persist an existing object type or create one through NEW."""
        if not self.project:
            return
        device = next(
            (item for item in self.project.devices if item.device_id == device_id), None
        )
        if device is None:
            return
        previous = device.object_type or _footprint_family(device.footprint_library)
        object_type = selected
        if selected == "NEW":
            object_type, accepted = QInputDialog.getText(
                self,
                "New object type",
                "Type name:",
            )
            object_type = object_type.strip()
            if not accepted or not object_type:
                combo.blockSignals(True)
                combo.setCurrentText(previous)
                combo.blockSignals(False)
                return
        self.project.devices = [
            (
                replace(item, object_type=object_type)
                if item.device_id == device_id
                else item
            )
            for item in self.project.devices
        ]
        self._dirty = True
        self._schematic_view.set_project(self.project)
        self._update_title()
        QTimer.singleShot(0, self._refresh_bom_table)

    def _show_schematic_device_menu(self, device_id: str) -> None:
        """Show actions for a component selected in the schematic."""
        if not self.project:
            return
        device = next(
            (item for item in self.project.devices if item.device_id == device_id),
            None,
        )
        if device is None:
            return
        menu = QMenu(self)
        set_id_action = menu.addAction("Set Component…")
        rotate_action = menu.addAction("Rotate 90°")
        action = menu.exec(QCursor.pos())
        if action == set_id_action:
            reference, accepted = QInputDialog.getText(
                self,
                "Set component",
                f"Component ID for {device.reference}:",
                text=device.reference,
            )
            if accepted:
                self._rename_device(device_id, reference)
        elif action == rotate_action:
            self.project.devices = [
                (
                    replace(
                        item,
                        schematic_rotation=(item.schematic_rotation + 90.0) % 360.0,
                    )
                    if item.device_id == device_id
                    else item
                )
                for item in self.project.devices
            ]
            self._dirty = True
            self._schematic_view.set_project(self.project)
            self._update_title()

    @Slot(str, float, float)
    def _schematic_layout_changed(self, _device_id: str, _x: float, _y: float) -> None:
        """Mark schematic drag/rotation changes for project persistence."""
        self._dirty = True
        self._update_title()

    def _terminal_net(self, terminal: tuple[str, str, str | None]) -> str | None:
        """Return the net currently assigned to one schematic terminal."""
        if not self.project:
            return None
        kind, object_id, number = terminal
        if kind == "pad":
            pad = next(
                (item for item in self.project.pads if item.pad_id == object_id), None
            )
            return pad.net if pad else None
        device = next(
            (item for item in self.project.devices if item.device_id == object_id), None
        )
        if device is None:
            return None
        pin = next((item for item in device.pins if item.number == number), None)
        return pin.net_id if pin else None

    @staticmethod
    def _validated_terminal(value: object) -> tuple[str, str, str | None] | None:
        """Validate a terminal identifier received from a Qt object signal."""
        if not isinstance(value, tuple) or len(value) != 3:
            return None
        kind, object_id, number = value
        if kind not in {"pin", "pad"} or not isinstance(object_id, str):
            return None
        if number is not None and not isinstance(number, str):
            return None
        return kind, object_id, number

    @Slot(object)
    def _select_schematic_terminal(self, value: object) -> None:
        """Toggle the net highlighted by a plain terminal click."""
        if not self.project:
            return
        terminal = self._validated_terminal(value)
        if terminal is None:
            return
        if terminal == self._selected_schematic_terminal:
            self._selected_schematic_terminal = None
            self._selected_net = None
            self._selected_pad_id = None
        else:
            self._selected_schematic_terminal = terminal
            self._selected_net = self._terminal_net(terminal)
            kind, object_id, number = terminal
            if kind == "pad":
                self._selected_pad_id = object_id
            else:
                pad = next(
                    (
                        item
                        for item in self.project.pads
                        if item.device_id == object_id and item.number == number
                    ),
                    None,
                )
                self._selected_pad_id = pad.pad_id if pad else None
        self._schematic_view.set_selected_net(self._selected_net)

    def _assign_schematic_terminal_net(
        self, terminal: tuple[str, str, str | None], net: str | None
    ) -> None:
        """Assign one explicit net and synchronize its physical/generated pad."""
        if not self.project:
            return
        kind, object_id, number = terminal
        if kind == "pad":
            self.project.pads = [
                replace(pad, net=net) if pad.pad_id == object_id else pad
                for pad in self.project.pads
            ]
        else:
            self.project.devices = [
                (
                    replace(
                        device,
                        pins=[
                            replace(pin, net_id=net) if pin.number == number else pin
                            for pin in device.pins
                        ],
                    )
                    if device.device_id == object_id
                    else device
                )
                for device in self.project.devices
            ]
            self.project.pads = [
                (
                    replace(pad, net=net)
                    if pad.device_id == object_id and pad.number == number
                    else pad
                )
                for pad in self.project.pads
            ]
        self._dirty = True
        self._schematic_view.set_project(self.project)
        self._refresh_net_table()
        self._update_title()

    @Slot(object)
    def _edit_schematic_terminal_net(self, value: object) -> None:
        """Open direct net-name editing for a Shift-right-clicked terminal."""
        terminal = self._validated_terminal(value)
        if terminal is None:
            return
        current = self._terminal_net(terminal) or ""
        net, accepted = QInputDialog.getText(
            self,
            "Connect terminal to net",
            "Net name:",
            text=current,
        )
        if accepted:
            self._assign_schematic_terminal_net(terminal, net.strip() or None)

    @Slot(object)
    def _show_schematic_terminal_menu(self, value: object) -> None:
        """Show available actions for a right-clicked schematic terminal."""
        terminal = self._validated_terminal(value)
        if terminal is None:
            return
        if self._pad_menu is not None:
            self._pad_menu.close()
        menu = QMenu(self)
        connect_action = menu.addAction("Connect to net…")
        disconnect_action = menu.addAction("Disconnect")
        disconnect_action.setEnabled(self._terminal_net(terminal) is not None)
        connect_action.triggered.connect(
            lambda: self._edit_schematic_terminal_net(terminal)
        )
        disconnect_action.triggered.connect(
            lambda: self._assign_schematic_terminal_net(terminal, None)
        )
        kind, object_id, number = terminal
        if kind == "pin" and number is not None:
            edit_action = menu.addAction("Edit function…")
            edit_action.triggered.connect(
                lambda: self._edit_component_pin(object_id, number)
            )
        elif kind == "pad":
            delete_action = menu.addAction("Delete pad")
            delete_action.triggered.connect(lambda: self._delete_pad(object_id))
        menu.aboutToHide.connect(lambda menu=menu: self._pad_menu_closed(menu))
        self._pad_menu = menu
        menu.popup(QCursor.pos())

    def _next_generic_net_name(self) -> str:
        """Return the first unused automatic net name N1, N2, etc."""
        if not self.project:
            return "N1"
        used = {
            net.casefold()
            for net in (
                [pad.net for pad in self.project.pads]
                + [pin.net_id for device in self.project.devices for pin in device.pins]
            )
            if net
        }
        index = 1
        while f"n{index}" in used:
            index += 1
        return f"N{index}"

    @Slot(object, object)
    def _connect_schematic_terminals(self, first: object, second: object) -> None:
        """Create or merge a net between two selected terminals."""
        if not self.project:
            return
        first = self._validated_terminal(first)
        second = self._validated_terminal(second)
        if first is None or second is None:
            return
        first_net = self._terminal_net(first)
        second_net = self._terminal_net(second)
        target_net = first_net or second_net or self._next_generic_net_name()
        merged_net = second_net if first_net and second_net != first_net else None
        terminals = {first, second}
        self.project.pads = [
            replace(
                pad,
                net=(
                    target_net
                    if ("pad", pad.pad_id, None) in terminals
                    or (merged_net is not None and pad.net == merged_net)
                    else pad.net
                ),
            )
            for pad in self.project.pads
        ]
        devices: list[Device] = []
        for device in self.project.devices:
            pins = [
                replace(
                    pin,
                    net_id=(
                        target_net
                        if ("pin", device.device_id, pin.number) in terminals
                        or (merged_net is not None and pin.net_id == merged_net)
                        else pin.net_id
                    ),
                )
                for pin in device.pins
            ]
            devices.append(replace(device, pins=pins))
        self.project.devices = devices
        pin_terminals = {
            (object_id, number)
            for kind, object_id, number in terminals
            if kind == "pin"
        }
        self.project.pads = [
            (
                replace(pad, net=target_net)
                if pad.device_id and (pad.device_id, pad.number) in pin_terminals
                else pad
            )
            for pad in self.project.pads
        ]
        self._selected_net = target_net
        self._dirty = True
        current_tab = self._tabs.currentIndex()
        self._refresh_views()
        self._tabs.setCurrentIndex(current_tab)
        self._schematic_view.set_selected_net(self._selected_net)
        self._update_title()
        self.statusBar().showMessage(f"Connected terminals to net {target_net}.", 3000)

    def _refresh_overlay(self, images: dict[str, QPixmap]) -> None:
        """Compose top and bottom images in the same CMS top-view orientation."""
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
            bottom = bottom.scaled(
                base.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.setOpacity(0.5)
            painter.drawPixmap(0, 0, bottom)
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
        if self._pad_display_mode == "pads":
            canvas = QPixmap(pixmap.size())
            canvas.fill(QColor(32, 33, 36))
            pixmap = canvas
        else:
            pixmap = QPixmap(pixmap)
        if self._pad_display_mode == "image":
            return pixmap
        return pixmap

    def _draw_devices(self, pixmap: QPixmap, side: str) -> None:
        """Draw every persisted KiCad footprint at calibrated physical scale."""
        if not self.project or not self.store:
            return
        image = next(
            (asset for asset in self.project.images if asset.side == side), None
        )
        if image is None:
            return
        pixels_per_mm = image.measured_pixels_per_mm(pixmap.width(), pixmap.height())
        if pixels_per_mm is None:
            pixels_per_mm = image.pixels_per_mm
        if pixels_per_mm is None:
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
                pixels_per_mm,
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

    def _rebuild_device_pads(self, side: str, pixmap: QPixmap) -> None:
        """Recalculate generated pads after an image is recalibrated."""
        if not self.project or pixmap.isNull():
            return
        image = next(
            (asset for asset in self.project.images if asset.side == side), None
        )
        if image is None:
            return
        pixels_per_mm = image.measured_pixels_per_mm(pixmap.width(), pixmap.height())
        if pixels_per_mm is None:
            return
        for device in [item for item in self.project.devices if item.side == side]:
            footprint = self._footprint_for_device(device)
            if footprint is None:
                continue
            try:
                placed_pads = place_footprint_pads(
                    footprint,
                    side,
                    device.x,
                    device.y,
                    device.rotation,
                    pixmap.width(),
                    pixmap.height(),
                    pixels_per_mm,
                )
            except KiCadFormatError as error:
                LOGGER.warning(
                    "Cannot rebuild pads device=%s error=%s",
                    device.reference,
                    error,
                )
                continue
            existing = {
                pad.number: pad
                for pad in self.project.pads
                if pad.device_id == device.device_id and pad.number is not None
            }
            rebuilt = []
            for placed in placed_pads:
                previous = existing.get(placed.number)
                if previous is None:
                    rebuilt.append(
                        Pad(
                            f"{device.reference}.{placed.number}",
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
                    )
                else:
                    rebuilt.append(
                        replace(
                            previous,
                            side=side,
                            x=placed.x,
                            y=placed.y,
                            width=placed.width,
                            height=placed.height,
                            shape=placed.shape,
                            rotation=placed.rotation,
                        )
                    )
            self.project.pads = [
                pad for pad in self.project.pads if pad.device_id != device.device_id
            ] + rebuilt
            LOGGER.info(
                "Rebuilt device pads reference=%s pixels_per_mm=%.6f pads=%s",
                device.reference,
                pixels_per_mm,
                len(rebuilt),
            )

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

    def _draw_pads(  # pylint: disable=too-many-locals,too-many-statements
        self, pixmap: QPixmap, side: str
    ) -> None:
        """Draw persisted pad markers over one board-side image."""
        if (
            self._pad_display_mode == "image"
            or not self.project
            or not self.project.pads
        ):
            return
        painter = QPainter(pixmap)
        radius = max(5, min(pixmap.width(), pixmap.height()) // 100)
        pads = [pad for pad in self.project.pads if pad.side == side]
        painter.setOpacity(0.45)
        for pad in pads:
            pin_one = pad.device_id is not None and pad.number == "1"
            painter.setBrush(Qt.GlobalColor.yellow if pin_one else Qt.GlobalColor.red)
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
            painter.setOpacity(0.45)
        painter.end()

    def _draw_connections(self, pixmap: QPixmap, side: str) -> None:
        """Draw traces last: above image, footprint, and pad layers."""
        if (
            self._pad_display_mode == "image"
            or not self.project
            or not self._selected_net
        ):
            return
        pads = [
            pad
            for pad in self.project.pads
            if pad.side == side and pad.net == self._selected_net
        ]
        if len(pads) < 2:
            return
        centers = {
            pad.pad_id: QPoint(
                round((pad.x + pad.width / 2) * (pixmap.width() - 1)),
                round((pad.y + pad.height / 2) * (pixmap.height() - 1)),
            )
            for pad in pads
        }
        origin_id = (
            self._selected_pad_id
            if self._selected_pad_id in centers
            else pads[0].pad_id
        )
        origin = centers[origin_id]
        painter = QPainter(pixmap)
        painter.setPen(
            QPen(
                Qt.GlobalColor.white,
                max(2, min(pixmap.width(), pixmap.height()) // 200),
            )
        )
        for pad_id, target in centers.items():
            if pad_id != origin_id:
                painter.drawLine(origin, target)
        painter.end()

    def _update_title(self) -> None:
        name = self.project.project_name if self.project else "No project"
        self.setWindowTitle(f"Tnasrevner — {name}{' *' if self._dirty else ''}")


def main() -> int:
    """Run Tnasrevner GUI."""
    # GNOME/Wayland matches windows to desktop entries through app_id.
    os.environ.setdefault("QT_WAYLAND_APP_ID", "tnasrevner")
    app = QApplication(sys.argv)
    app.setApplicationName("tnasrevner")
    app.setApplicationDisplayName("Tnasrevner")
    app.setDesktopFileName("tnasrevner")
    app.setWindowIcon(_application_icon())
    _configure_logging()
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
