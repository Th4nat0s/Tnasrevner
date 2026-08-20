"""Application GUI support, dialogs, constants, and logging."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,invalid-name,unused-import
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


def _application_icon() -> QIcon:
    """Load application icon from package data."""
    return QIcon(str(Path(__file__).parent.parent / "assets" / "tnasrevner.svg"))


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
_NATURAL_IDENTIFIER_PARTS = re.compile(r"(\d+)")
_RECENT_FOOTPRINTS_KEY = "kicad/recent-footprints"
_REFERENCE_PREFIX_KEY = "kicad/reference-prefix"
_LAST_PROJECT_DIRECTORY_KEY = "projects/last-directory"
_MAX_RECENT_FOOTPRINTS = 5
_CONNECTIONS_TAB = 4
_NETS_TAB = 5
_BOM_TAB = 6
_SCHEMATIC_TAB = 7
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


def _natural_sort_key(identifier: str) -> tuple[tuple[int, str | int], ...]:
    """Return a case-insensitive natural-sort key for an identifier.

    Numeric portions are compared as integers, so ``C1.2`` sorts before
    ``C1.10`` while textual portions and suffixes remain in their original
    order.

    Args:
        identifier: Pad, pin, or other display identifier to sort.

    Returns:
        Comparable token pairs containing text and integer portions.
    """
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in _NATURAL_IDENTIFIER_PARTS.split(identifier)
        if part
    )


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
    """Collect the project name needed to create a project."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New project")
        self.project_name = QLineEdit()
        self.description = QLineEdit()
        form = QFormLayout(self)
        form.addRow("Project name", self.project_name)
        form.addRow("Description", self.description)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self) -> None:
        """Reject empty names before closing the dialog."""
        if not self.project_name.text().strip():
            QMessageBox.warning(self, "Missing name", "Enter a project name.")
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
