"""KiCad footprint selection, caching, previews, and painting."""

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

from .app_support import (
    LOGGER,
    _MAX_RECENT_FOOTPRINTS,
    _footprint_family,
    _reference_sort_key,
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
        self._pad_count.valueChanged.connect(
            lambda _value: self._refresh_results(self._search.text())
        )
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

    def _reference_pad_count(self, reference: FootprintReference) -> int | None:
        """Return indexed pad count, falling back to parsing one footprint."""
        indexed = self._pad_counts.get(reference.identifier)
        if indexed is not None:
            return indexed if indexed >= 0 else None
        try:
            footprint = self._preview_cache.get(reference.identifier)
            if footprint is None:
                footprint = parse_footprint(
                    reference.path.read_bytes(), reference.library
                )
                self._preview_cache[reference.identifier] = footprint
        except (OSError, KiCadFormatError):
            return None
        return footprint.pad_count()

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
                if self._reference_pad_count(reference) == pad_count:
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
