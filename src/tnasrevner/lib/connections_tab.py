"""Connections and NET tabs, routing, names, pins, and terminal links."""

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


class ConnectionsTabMixin:
    """Provide connectionstab behavior to the main window."""

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
                    if column in (1, 2, 4, 5):
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

    def _sync_net_registry(self) -> None:
        """Keep registry entries aligned with assigned pads and pins."""
        if not self.project:
            return
        names = {
            net
            for net in (
                [pad.net for pad in self.project.pads]
                + [pin.net_id for device in self.project.devices for pin in device.pins]
            )
            if net
        }
        used = {name.casefold() for name in names}
        self.project.nets = [
            net for net in self.project.nets if net.name.casefold() in used
        ]
        if self._selected_net and self._selected_net.casefold() not in used:
            self._selected_net = None
        known = {net.name.casefold() for net in self.project.nets}
        for name in sorted(names):
            key = name.casefold()
            if key not in known:
                self.project.nets.append(Net(name=name))
                known.add(key)

    def _logical_net_terminals(self, net_name: str) -> set[tuple[str, str, str | None]]:
        """Return unique terminals attached to one NET.

        Args:
            net_name: NET name whose physical pads and component pins are inspected.

        Returns:
            Unique terminal identities; generated pad and matching pin count once.
        """
        if not self.project:
            return set()
        terminals = {
            self._pad_terminal(pad) for pad in self.project.pads if pad.net == net_name
        }
        terminals.update(
            ("pin", device.device_id, pin.number)
            for device in self.project.devices
            for pin in device.pins
            if pin.net_id == net_name
        )
        return terminals

    def _cleanup_single_terminal_nets(
        self, net_names: set[str] | None = None
    ) -> set[str]:
        """Remove NET assignments that no longer form a connection.

        Args:
            net_names: Optional NET names to inspect; all assigned names when omitted.

        Returns:
            Names removed because fewer than two unique terminals remained.
        """
        if not self.project:
            return set()
        candidates = net_names or {
            name
            for name in (
                [pad.net for pad in self.project.pads]
                + [pin.net_id for device in self.project.devices for pin in device.pins]
            )
            if name
        }
        removed = {
            name for name in candidates if len(self._logical_net_terminals(name)) < 2
        }
        if not removed:
            self._sync_net_registry()
            return set()
        self.project.pads = [
            replace(pad, net=None) if pad.net in removed else pad
            for pad in self.project.pads
        ]
        self.project.devices = [
            replace(
                device,
                pins=[
                    replace(pin, net_id=None) if pin.net_id in removed else pin
                    for pin in device.pins
                ],
            )
            for device in self.project.devices
        ]
        self.project.nets = [
            net for net in self.project.nets if net.name not in removed
        ]
        if self._selected_net in removed:
            self._selected_net = None
            self._selected_pad_id = None
        self._sync_net_registry()
        return removed

    def _refresh_nets_table(self) -> None:
        """Show one editable row per NET and its terminal count."""
        if not self.project:
            self._nets_table.setRowCount(0)
            return
        self._ensure_component_pins()
        self._sync_net_registry()
        counts = {
            net.name: sum(pad.net == net.name for pad in self.project.pads)
            + sum(
                pin.net_id == net.name
                for device in self.project.devices
                for pin in device.pins
            )
            for net in self.project.nets
        }
        nets = sorted(self.project.nets, key=lambda net: net.name.casefold())
        self._nets_table.blockSignals(True)
        try:
            self._nets_table.setRowCount(len(nets))
            for row, net in enumerate(nets):
                name_item = QTableWidgetItem(net.name)
                name_item.setData(Qt.ItemDataRole.UserRole, net.net_id)
                self._nets_table.setItem(row, 0, name_item)
                count_item = QTableWidgetItem(str(counts[net.name]))
                count_item.setFlags(count_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._nets_table.setItem(row, 1, count_item)
        finally:
            self._nets_table.blockSignals(False)

    def _nets_table_cell_changed(self, row: int, column: int) -> None:
        """Rename a NET from the summary table; connection count stays read-only."""
        if not self.project or column != 0:
            return
        item = self._nets_table.item(row, column)
        if item is None:
            return
        net_id = item.data(Qt.ItemDataRole.UserRole)
        net = next(
            (
                candidate
                for candidate in self.project.nets
                if candidate.net_id == net_id
            ),
            None,
        )
        if net is None:
            return
        new_name = item.text().strip()
        if not new_name or any(
            candidate.net_id != net.net_id
            and candidate.name.casefold() == new_name.casefold()
            for candidate in self.project.nets
        ):
            self._nets_table.blockSignals(True)
            item.setText(net.name)
            self._nets_table.blockSignals(False)
            self.statusBar().showMessage("NET name invalid or already used", 3000)
            return
        self._rename_net(net, new_name)

    def _rename_net(self, net: Net, new_name: str) -> None:
        """Rename NET registry entry and every pad/pin assignment."""
        old_name = net.name
        if old_name == new_name:
            return
        self.project.nets = [
            replace(item, name=new_name) if item.net_id == net.net_id else item
            for item in self.project.nets
        ]
        self.project.pads = [
            replace(pad, net=new_name) if pad.net == old_name else pad
            for pad in self.project.pads
        ]
        self.project.devices = [
            replace(
                device,
                pins=[
                    replace(pin, net_id=new_name) if pin.net_id == old_name else pin
                    for pin in device.pins
                ],
            )
            for device in self.project.devices
        ]
        if self._selected_net == old_name:
            self._selected_net = new_name
        self._dirty = True
        current_tab = self._tabs.currentIndex()
        self._refresh_views()
        self._tabs.setCurrentIndex(current_tab)
        self._schematic_view.set_selected_net(self._selected_net)
        self._update_title()
        self.statusBar().showMessage(f"NET renamed: {old_name} → {new_name}", 3000)

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
        if not self.project or column != 3:
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

    @Slot(str, float, float)
    def _schematic_layout_changed(self, _device_id: str, _x: float, _y: float) -> None:
        """Mark schematic drag/rotation changes for project persistence."""
        self._dirty = True
        self._update_title(record_history=False)

    @Slot(str)
    def _schematic_layout_started(self, _device_id: str) -> None:
        """Record the pre-drag state once before a component moves."""
        self._record_history()

    @Slot(str)
    def _schematic_layout_finished(self, _device_id: str) -> None:
        """Record one final state after a component drag is released."""
        self._record_history()
        self._update_title(record_history=False)

    @Slot()
    def _schematic_layout_optimized(self) -> None:
        """Mark an explicit schematic optimization as a project change."""
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
        if pin is None:
            return None
        if pin.net_id:
            return pin.net_id
        return next(
            (
                pad.net
                for pad in self.project.pads
                if pad.device_id == object_id and pad.number == number and pad.net
            ),
            None,
        )

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
        if self._connection_mode:
            self._append_connection_terminal(terminal)
            if self._pending_connection_terminals:
                self._schematic_view.set_connection_preview_terminal(terminal)
            return
        if terminal == self._selected_schematic_terminal:
            self._selected_schematic_terminal = None
            self._selected_net = None
            self._selected_pad_id = None
            self.statusBar().clearMessage()
        else:
            self._selected_schematic_terminal = terminal
            self._selected_net = self._terminal_net(terminal)
            kind, object_id, number = terminal
            if kind == "pad":
                self._selected_pad_id = object_id
                selected_label = next(
                    (pad.name for pad in self.project.pads if pad.pad_id == object_id),
                    "Pad",
                )
            else:
                device = next(
                    (
                        item
                        for item in self.project.devices
                        if item.device_id == object_id
                    ),
                    None,
                )
                selected_label = (
                    f"{device.reference}.{number}"
                    if device is not None
                    else f"Pin {number}"
                )
                pad = next(
                    (
                        item
                        for item in self.project.pads
                        if item.device_id == object_id and item.number == number
                    ),
                    None,
                )
                self._selected_pad_id = pad.pad_id if pad else None
            self.statusBar().showMessage(
                f"{selected_label} | Net: {self._selected_net or '—'}"
            )
        self._schematic_view.set_selected_net(self._selected_net)

    def _assign_schematic_terminal_net(
        self, terminal: tuple[str, str, str | None], net: str | None
    ) -> None:
        """Assign one explicit net and synchronize its physical/generated pad."""
        if not self.project:
            return
        previous_net = self._terminal_net(terminal)
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
        if previous_net is not None and previous_net != net:
            self._cleanup_single_terminal_nets({previous_net})
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
            pad = next(
                (item for item in self.project.pads if item.pad_id == object_id),
                None,
            )
            if pad is not None and pad.device_id is None:
                glue_action = menu.addAction(
                    "Unglue" if pad.schematic_glued else "Glue"
                )
                glue_action.triggered.connect(
                    lambda: self._set_schematic_pad_glued(
                        object_id, not pad.schematic_glued
                    )
                )
            delete_action = menu.addAction("Delete pad")
            delete_action.triggered.connect(lambda: self._delete_pad(object_id))
        menu.aboutToHide.connect(lambda menu=menu: self._pad_menu_closed(menu))
        self._pad_menu = menu
        menu.popup(QCursor.pos())

    def _set_schematic_pad_glued(self, pad_id: str, glued: bool) -> None:
        """Persist fixed-position state for one independent schematic pad.

        Args:
            pad_id: Stable pad identifier.
            glued: Whether schematic dragging is blocked.
        """
        if not self.project:
            return
        if not any(
            pad.pad_id == pad_id and pad.device_id is None for pad in self.project.pads
        ):
            return
        self.project.pads = [
            replace(pad, schematic_glued=glued) if pad.pad_id == pad_id else pad
            for pad in self.project.pads
        ]
        self._dirty = True
        self._schematic_view.set_project(self.project)
        self._update_title()

    def _next_connection_net_name(self) -> str:
        """Return first unused NT1..NT9999 name."""
        if not self.project:
            return "NT1"
        used = {
            net.casefold()
            for net in (
                [pad.net for pad in self.project.pads]
                + [pin.net_id for device in self.project.devices for pin in device.pins]
            )
            if net
        }
        for index in range(1, 10_000):
            candidate = f"NT{index}"
            if candidate.casefold() not in used:
                return candidate
        return "NT9999"

    def _connect_terminals(self, values: tuple[object, ...]) -> str | None:
        """Create one shared net for all selected pads/pins."""
        if not self.project:
            return None
        terminals = tuple(
            dict.fromkeys(
                terminal
                for value in values
                if (terminal := self._validated_terminal(value)) is not None
            )
        )
        if len(terminals) < 2:
            return None
        view_state = self._capture_history_view_state()
        existing_nets = tuple(
            dict.fromkeys(
                self._terminal_net(terminal)
                for terminal in terminals
                if self._terminal_net(terminal) is not None
            )
        )
        target_net = (
            existing_nets[0] if existing_nets else self._next_connection_net_name()
        )
        merged_nets = set(existing_nets)
        terminal_set = set(terminals)
        self.project.pads = [
            replace(
                pad,
                net=(
                    target_net
                    if pad.net in merged_nets
                    or ("pad", pad.pad_id, None) in terminal_set
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
                        if pin.net_id in merged_nets
                        or ("pin", device.device_id, pin.number) in terminal_set
                        else pin.net_id
                    ),
                )
                for pin in device.pins
            ]
            devices.append(replace(device, pins=pins))
        self.project.devices = devices
        pin_terminals = {
            (object_id, number)
            for kind, object_id, number in terminal_set
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
        self._refresh_views()
        self._restore_history_view_state(view_state)
        self._schematic_view.set_selected_net(self._selected_net)
        self._update_title()
        self.statusBar().showMessage(f"Connected terminals to net {target_net}.", 3000)
        return target_net

    @Slot(object, object)
    def _connect_schematic_terminals(self, first: object, second: object) -> None:
        """Compatibility wrapper for direct two-terminal net actions."""
        self._connect_terminals((first, second))
