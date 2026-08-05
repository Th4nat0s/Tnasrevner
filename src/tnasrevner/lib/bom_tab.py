"""BOM tab rendering, editing, metadata, and component menus."""

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


class BomTabMixin:
    """Provide bomtab behavior to the main window."""

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
        dialog = QInputDialog(self)
        dialog.setWindowTitle(label)
        dialog.setLabelText(f"{label} for {device.reference}:")
        dialog.setTextValue(getattr(device, field))
        if field == "datasheet":
            dialog.setMinimumWidth(700)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._set_device_text(device_id, field, dialog.textValue())

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
