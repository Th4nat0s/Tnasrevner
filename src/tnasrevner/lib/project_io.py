"""Project creation, opening, saving, and close confirmation."""

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
    FootprintDefinition,
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


class ProjectIOMixin:
    """Provide projectio behavior to the main window."""

    def new_project(self) -> None:
        """Create an empty `.revp` project file."""
        if not self._confirm_pending_changes():
            return
        dialog = ProjectDetailsDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        project_name = dialog.project_name.text()
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", project_name).strip("._")
        safe_name = safe_name or "project"
        project_path = self._last_project_directory() / f"{safe_name}.revp"
        suffix = 2
        while project_path.exists():
            project_path = self._last_project_directory() / f"{safe_name}-{suffix}.revp"
            suffix += 1
        self._remember_project_directory(project_path)
        self.store = ProjectStore(project_path)
        description = dialog.description.text().strip() or project_name
        self.project = ProjectDocument(project_name, description)
        self._project_needs_save_as = True
        self._image_cache.clear()
        self._device_footprint_cache.clear()
        self._cancel_device_placement()
        self._dirty = True
        self._reset_history()
        self._refresh_views()
        self._update_title()
        self.manage_picture()

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
        self._project_needs_save_as = False
        self._load_history_backup()
        self._image_cache.clear()
        self._device_footprint_cache.clear()
        self._cancel_device_placement()
        self._refresh_views()
        if self._tabs.currentIndex() == _SCHEMATIC_TAB:
            QTimer.singleShot(0, self._restore_schematic_viewport)
        self._update_title()

    def _capture_schematic_viewport(self) -> None:
        """Persist the current schematic zoom and scroll positions in memory."""
        if not self.project:
            return
        zoom, pan_x, pan_y = self._schematic_view.view_state()
        display = self.project.display
        state = (zoom, float(pan_x), float(pan_y))
        previous = (
            display.schematic_zoom,
            display.schematic_pan_x,
            display.schematic_pan_y,
        )
        if previous == state:
            return
        display.schematic_zoom, display.schematic_pan_x, display.schematic_pan_y = state
        self._dirty = True
        self._update_title(record_history=False)

    def _schematic_viewport_state(self) -> tuple[float, int, int] | None:
        """Return persisted schematic viewport or no-state legacy fallback."""
        if not self.project:
            return None
        display = self.project.display
        if display.schematic_zoom is None:
            return None
        return (
            display.schematic_zoom,
            round(display.schematic_pan_x or 0.0),
            round(display.schematic_pan_y or 0.0),
        )

    def _restore_schematic_viewport(self) -> None:
        """Restore saved schematic viewport, fitting only legacy projects."""
        state = self._schematic_viewport_state()
        if state is None:
            self._schematic_view.fit_overview()
            self._capture_schematic_viewport()
            return
        self._schematic_view.apply_view_state(state)

    def _handle_tab_changed(self, index: int) -> None:
        """Capture schematic viewport on tab changes and restore on entry."""
        if self._last_tab_index == _SCHEMATIC_TAB:
            self._capture_schematic_viewport()
        self._last_tab_index = index
        self._update_view_tools(index)
        if index == _SCHEMATIC_TAB:
            QTimer.singleShot(0, self._restore_schematic_viewport)

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
        if self._project_needs_save_as:
            return self.save_project_as()
        return self._save_to_store(self.store)

    def save_project_as(self) -> bool:
        """Save current project to a selected archive without changing source first."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return False
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save project as",
            str(self._last_project_directory()),
            "Tnasrevner project (*.revp)",
        )
        if not path:
            return False
        project_path = Path(path)
        if project_path.suffix.lower() != ".revp":
            project_path = project_path.with_suffix(".revp")
        if project_path != self.store.path and project_path.exists():
            answer = QMessageBox.question(
                self,
                "Replace project?",
                f"Replace existing project {project_path.name}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        new_store = ProjectStore(project_path)
        try:
            self._copy_project_assets(new_store)
        except (OSError, ProjectFormatError) as error:
            QMessageBox.critical(self, "Save As failed", str(error))
            return False
        if not self._save_to_store(new_store):
            return False
        self.store = new_store
        self._project_needs_save_as = False
        self._remember_project_directory(project_path)
        return True

    def _copy_project_assets(self, target: ProjectStore) -> None:
        """Copy archive and on-disk assets referenced by current project."""
        if not self.project or not self.store:
            return
        self._repair_footprint_references()
        self.store.copy_pending_assets_to(target)
        paths = {
            path
            for image in self.project.images
            for path in (image.path, image.original_path)
            if path
        }
        for definition in self.project.footprint_definitions:
            self._copy_footprint_asset(target, definition)
        paths.update(
            device.footprint_path
            for device in self.project.devices
            if not any(
                definition.definition_id == device.footprint_definition_id
                for definition in self.project.footprint_definitions
            )
        )
        for path in paths:
            target.write_asset(path, self.store.read_asset(path))

    def _repair_footprint_references(self) -> None:
        """Align device footprint references with shared project definitions.

        Missing or stale device paths are repaired by matching library/name, then
        by loading the footprint from the local KiCad cache when necessary.

        Raises:
            ProjectFormatError: If a referenced footprint cannot be recovered.
        """
        if not self.project or not self.store:
            return
        definitions = self.project.footprint_definitions
        repaired_devices: list[Device] = []
        for device in self.project.devices:
            definition = next(
                (
                    item
                    for item in definitions
                    if item.definition_id == device.footprint_definition_id
                    or (
                        item.library == device.footprint_library
                        and item.name == device.footprint_name
                    )
                ),
                None,
            )
            if definition is None:
                content = self._read_device_footprint(device)
                definition = self.store.register_footprint(
                    self.project,
                    device.footprint_library,
                    device.footprint_name,
                    device.source_revision,
                    content,
                )
                definitions = self.project.footprint_definitions
            repaired_devices.append(
                replace(
                    device,
                    footprint_path=definition.path,
                    footprint_definition_id=definition.definition_id,
                )
            )
        self.project.devices = repaired_devices

    def _read_device_footprint(self, device: Device) -> bytes:
        """Read one device footprint from the project or KiCad cache.

        Args:
            device: Device whose footprint content is required.

        Returns:
            Raw KiCad footprint bytes.

        Raises:
            ProjectFormatError: If neither project storage nor cache contains it.
        """
        if not self.store:
            raise ProjectFormatError("project store is unavailable")
        try:
            return self.store.read_asset(device.footprint_path)
        except ProjectFormatError as source_error:
            try:
                reference = next(
                    item
                    for item in self._footprint_cache.catalog()
                    if item.library == device.footprint_library
                    and item.name == device.footprint_name
                )
                _footprint, content = self._footprint_cache.load(reference)
                return content
            except (KiCadCacheError, KiCadFormatError, StopIteration) as cache_error:
                raise ProjectFormatError(
                    f"cannot recover footprint asset: {device.footprint_library}:"
                    f"{device.footprint_name} ({device.footprint_path})"
                ) from cache_error

    def _copy_footprint_asset(
        self, target: ProjectStore, definition: FootprintDefinition
    ) -> None:
        """Copy one footprint, recovering it from the KiCad cache if needed.

        Args:
            target: Destination project store.
            definition: Project footprint definition with path, library, and name.

        Raises:
            ProjectFormatError: If the source and cache both lack the footprint.
        """
        if not self.store or not self.project:
            return
        try:
            content = self.store.read_asset(definition.path)
        except ProjectFormatError:
            try:
                reference = next(
                    item
                    for item in self._footprint_cache.catalog()
                    if item.library == definition.library
                    and item.name == definition.name
                )
                _footprint, content = self._footprint_cache.load(reference)
            except (KiCadCacheError, KiCadFormatError, StopIteration) as cache_error:
                raise ProjectFormatError(
                    f"cannot recover footprint asset: {definition.library}:"
                    f"{definition.name} ({definition.path})"
                ) from cache_error
            LOGGER.warning(
                "Recovered missing footprint asset path=%s from KiCad cache",
                definition.path,
            )
        target.write_asset(definition.path, content)

    def _save_to_store(self, store: ProjectStore) -> bool:
        """Write current project to one store and update UI state on success."""
        self._capture_schematic_viewport()
        self.project.display.mode = (
            "top",
            "bottom",
            "side_by_side",
            "both",
            "nets",
            "net_summary",
            "bom",
            "schematic",
        )[self._tabs.currentIndex()]
        if self._tabs.currentIndex() >= _CONNECTIONS_TAB:
            zoom, pan_x, pan_y = (
                self.project.display.zoom,
                self.project.display.pan_x,
                self.project.display.pan_y,
            )
        else:
            display_view = self._active_views()[0]
            zoom, pan_x, pan_y = display_view.view_state()
        if self._tabs.currentIndex() < _CONNECTIONS_TAB:
            self.project.display.zoom = zoom
            self.project.display.pan_x = pan_x
            self.project.display.pan_y = pan_y
        progress = QProgressDialog("Saving project…", "", 0, 1, self)
        progress.setWindowTitle("Save project")
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        QApplication.processEvents()

        def report(label: str, current: int, total: int) -> None:
            progress.setLabelText(label)
            progress.setRange(0, max(1, total))
            progress.setValue(current)
            QApplication.processEvents()

        try:
            report("Preparing save", 0, 1)
            self._repair_footprint_references()
            self._write_history_backup()
            store.save(self.project, progress=report)
        except (OSError, ProjectFormatError) as error:
            QMessageBox.critical(self, "Save failed", str(error))
            return False
        finally:
            progress.close()
            progress.deleteLater()
        self._dirty = False
        self._update_title()
        return True

    def close_project(self) -> None:
        """Close current project after resolving pending changes."""
        if not self._confirm_pending_changes():
            return
        self.project = None
        self.store = None
        self._project_needs_save_as = False
        self._image_cache.clear()
        self._device_footprint_cache.clear()
        self._cancel_device_placement()
        self._dirty = False
        self._reset_history()
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
            application = QApplication.instance()
            if application is not None:
                application.removeEventFilter(self)
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
        application = QApplication.instance()
        if application is not None:
            application.removeEventFilter(self)
        event.accept()
