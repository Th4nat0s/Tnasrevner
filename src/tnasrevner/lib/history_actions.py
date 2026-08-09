"""Undo, redo, keyboard actions, and history persistence."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,invalid-name,unused-import
# pylint: disable=too-many-instance-attributes,duplicate-code
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


class HistoryActionsMixin:
    """Provide historyactions behavior to the main window."""

    def _history_snapshot(self) -> dict:
        """Return the JSON project state for undo/redo."""
        if not self.project:
            return {}
        self._capture_schematic_viewport()
        return {"project": self.project.to_dict()}

    @staticmethod
    def _history_signature(snapshot: dict) -> str:
        """Return a stable comparison key for one history state."""
        return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))

    def _record_history(self) -> None:
        """Record a changed project state, retaining the latest 50 states."""
        if self._history_restoring or not self.project or not self.store:
            return
        snapshot = self._history_snapshot()
        signature = self._history_signature(snapshot)
        if self._history and self._history_signature(self._history[-1]) == signature:
            return
        if self._history_index < len(self._history) - 1:
            self._history = self._history[: self._history_index + 1]
        self._history.append(snapshot)
        self._history = self._history[-50:]
        self._history_index = len(self._history) - 1
        self._history_backup_dirty = True
        self._update_history_buttons()

    def _reset_history(self) -> None:
        """Start a fresh history for the current project."""
        self._history = []
        self._history_index = -1
        self._record_history()
        self._update_history_buttons()

    def _update_history_buttons(self) -> None:
        """Enable palette Undo/Redo buttons according to the current cursor."""
        if hasattr(self, "_undo_button"):
            self._undo_button.setEnabled(self._history_index > 0)
            self._redo_button.setEnabled(
                0 <= self._history_index < len(self._history) - 1
            )

    def _write_history_backup(self) -> None:
        """Embed the undo journal in the project asset backup."""
        if not self.store or not self._history or not self._history_backup_dirty:
            return
        payload = {
            "version": 1,
            "index": self._history_index,
            "states": self._history,
        }
        self.store.write_asset(
            "assets/.undo-history.json",
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )
        self._history_backup_dirty = False

    def _load_history_backup(self) -> None:
        """Restore the undo journal embedded in a project archive."""
        self._history = []
        self._history_index = -1
        self._history_backup_dirty = False
        if not self.store:
            return
        try:
            payload = json.loads(
                self.store.read_asset("assets/.undo-history.json").decode("utf-8")
            )
            states = payload.get("states", [])
            index = int(payload.get("index", -1))
            if isinstance(states, list) and all(
                isinstance(item, dict) for item in states
            ):
                legacy_assets = any("assets" in item for item in states[-50:])
                self._history = [
                    {"project": item["project"]}
                    for item in states[-50:]
                    if isinstance(item.get("project"), dict)
                ]
                self._history_index = max(-1, min(index, len(self._history) - 1))
                self._history_backup_dirty = legacy_assets
        except (
            ProjectFormatError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            pass
        if not self._history:
            self._record_history()
        else:
            self._history_backup_dirty = False
        self._update_history_buttons()

    def _restore_history_state(self, snapshot: dict) -> None:
        """Restore project and assets from one undo snapshot."""
        if not self.store:
            return
        project_data = snapshot.get("project")
        assets = snapshot.get("assets", {})
        if not isinstance(project_data, dict) or not isinstance(assets, dict):
            return
        try:
            project = ProjectDocument.from_dict(project_data)
            for path, encoded in assets.items():
                self.store.write_asset(path, base64.b64decode(encoded))
        except (ProjectFormatError, ValueError, TypeError):
            return
        self._history_restoring = True
        try:
            self.project = project
            self._image_cache.clear()
            self._device_footprint_cache.clear()
            self._dirty = True
            self._refresh_views()
            self._update_title()
        finally:
            self._history_restoring = False
        self._update_history_buttons()

    def _capture_history_view_state(self) -> tuple:
        """Capture the current tab and viewport without project content."""
        tab = self._tabs.currentIndex()
        if tab == _SCHEMATIC_TAB:
            return tab, self._schematic_view.view_state()
        views = self._active_views()
        return tab, views[0].view_state() if views else None

    def _restore_history_view_state(self, state: tuple) -> None:
        """Restore the tab and viewport after an undo or redo refresh."""
        tab, view_state = state
        self._tabs.setCurrentIndex(tab)
        if tab == _SCHEMATIC_TAB and view_state is not None:
            self._schematic_view.apply_view_state(view_state)
        elif view_state is not None:
            self._apply_active_view_state(view_state)

    def undo(self) -> None:
        """Undo the latest recorded project action."""
        if self._history_index <= 0:
            return
        view_state = self._capture_history_view_state()
        self._history_index -= 1
        self._history_backup_dirty = True
        self._restore_history_state(self._history[self._history_index])
        self._restore_history_view_state(view_state)
        self.statusBar().showMessage("Undo", 1500)

    def redo(self) -> None:
        """Redo the next recorded project action."""
        if self._history_index >= len(self._history) - 1:
            return
        view_state = self._capture_history_view_state()
        self._history_index += 1
        self._history_backup_dirty = True
        self._restore_history_state(self._history[self._history_index])
        self._restore_history_view_state(view_state)
        self.statusBar().showMessage("Redo", 1500)

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

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """Commit the current terminal group when Shift is released."""
        if (
            self._connection_mode
            and event.type() == QEvent.Type.KeyRelease
            and event.key() == Qt.Key.Key_Shift
        ):
            self._finish_connection_selection()
            return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """Switch board view with `T`, `B`, or a double press."""
        key = event.key()
        if (
            self._connection_mode
            and key == Qt.Key.Key_Shift
            and not event.isAutoRepeat()
        ):
            self._connection_trace_pairs = ()
            self._refresh_views_preserving_state()
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            if (
                getattr(self, "_delete_button", None) is not None
                and self._delete_button.isChecked()
            ):
                self._set_delete_mode(False)
                event.accept()
                return
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
            if self._connection_mode:
                self._exit_connection_mode()
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
        if hasattr(self, "_add_pad_button"):
            self._add_pad_button.setChecked(False)
        for view in (*self._views.values(), *self._side_views.values()):
            view.set_pad_placement(False)
        self.statusBar().clearMessage()
