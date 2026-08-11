"""Footprint cache, selection, placement, and component creation."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,invalid-name,unused-import
# pylint: disable=too-few-public-methods,duplicate-code
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


from . import app_support
from .app_support import (
    LOGGER,
    ProjectDetailsDialog,
    StartupDialog,
    _application_data_directory,
    _application_icon,
    _center_tool_icon,
    _pad_tool_icon,
    _save_tool_icon,
    _footprint_family,
    _footprint_family_key,
    _reference_sort_key,
    _DEVICE_REFERENCE,
    _NUMBERED_DEVICE_REFERENCE,
    _RECENT_FOOTPRINTS_KEY,
    _REFERENCE_PREFIX_KEY,
    _LAST_PROJECT_DIRECTORY_KEY,
    _MAX_RECENT_FOOTPRINTS,
    _CONNECTIONS_TAB,
    _NETS_TAB,
    _BOM_TAB,
    _SCHEMATIC_TAB,
    _DEFAULT_REFERENCE_PREFIXES,
)
from .footprints import (
    FootprintPickerDialog,
    KiCadCacheWorker,
    PendingDevice,
)
from .image_editor import ImageEditDialog
from .image_view import ImageView
from .schematic_tab import SchematicView


class FootprintActionsMixin:
    """Provide footprintactions behavior to the main window."""

    def _prefetch_footprints(self) -> None:
        """Download or refresh the KiCad footprint cache after application start."""
        if not self._footprint_cache.is_ready_and_fresh():
            self._start_kicad_cache_update(interactive=False)

    def add_device(self) -> None:
        """Select a footprint, collect its reference, and arm placement."""
        self._exit_connection_mode()
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
        default_prefix = _DEFAULT_REFERENCE_PREFIXES.get(family, "IC")
        remembered = self._settings.value(f"{_REFERENCE_PREFIX_KEY}/{family}")
        if isinstance(remembered, str) and re.fullmatch(r"[A-Za-z]+", remembered):
            return remembered
        if self.project:
            for device in reversed(self.project.devices):
                if _footprint_family_key(device.footprint_library) != family:
                    continue
                match = _NUMBERED_DEVICE_REFERENCE.fullmatch(device.reference)
                if match:
                    return match.group(1)
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
        self._add_device_button.setChecked(True)
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
            definition = self.store.register_footprint(
                self.project,
                pending.footprint.library,
                pending.footprint.name,
                pending.revision,
                pending.content,
            )
            device = Device(
                pending.reference,
                side,
                x,
                y,
                pending.footprint.library,
                pending.footprint.name,
                definition.path,
                pending.revision,
                device_id=device_id,
                rotation=pending.rotation,
                footprint_definition_id=definition.definition_id,
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
        except (KiCadFormatError, ProjectFormatError) as error:
            QMessageBox.warning(self, "Cannot place device", str(error))
            return
        self.project.devices.append(device)
        self.project.pads.extend(generated)
        self._device_footprint_cache[device.footprint_definition_id] = pending.footprint
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
        if self._tabs.currentIndex() == 3:
            self._refresh_views()
            restore_deferred = True
        else:
            self._refresh_device_side(side)
            restore_deferred = False
        self._tabs.setCurrentIndex(view_context[0])
        if restore_deferred:
            QTimer.singleShot(
                0,
                lambda context=view_context: self._restore_device_view_context(
                    *context
                ),
            )
        else:
            self._apply_active_view_state(view_context[1])
        self.statusBar().showMessage(
            f"Place {self._pending_device.reference}: left click to place, "
            "right click rotates 45°, Esc ends the series."
        )
        self._update_title(refresh_bom=False)

    def _restore_device_view_context(
        self, tab_index: int, state: tuple[float, float, float]
    ) -> None:
        """Restore placement tab and view state after board refresh callbacks."""
        self._tabs.setCurrentIndex(tab_index)
        self._apply_active_view_state(state)

    def _clear_device_previews(self) -> None:
        for view in (*self._views.values(), *self._side_views.values()):
            view.clear_device_placement()

    def _cancel_device_placement(self, clear_status: bool = True) -> None:
        """Cancel a pending footprint without changing project data."""
        self._pending_device = None
        self._clear_device_previews()
        if hasattr(self, "_add_device_button"):
            self._add_device_button.setChecked(False)
        if clear_status:
            self.statusBar().clearMessage()
