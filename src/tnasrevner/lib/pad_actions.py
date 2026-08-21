"""Pad placement, selection, menus, and component editing."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,invalid-name,unused-import
# pylint: disable=too-few-public-methods,too-many-instance-attributes,duplicate-code
# pylint: disable=too-many-lines
# pylint: disable=protected-access
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
    KiCadSymbolCache,
    parse_symbol_library,
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
    swap_two_pin_assignments,
    NC_NET,
    is_nc_net,
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


class PadActionsMixin:
    """Provide padactions behavior to the main window."""

    _view_restore_revision = 0

    def create_pad(self) -> None:
        """Start rectangle placement on the currently visible board view."""
        self._set_move_mode(None)
        self._exit_connection_mode()
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
        self._add_pad_button.setChecked(True)
        self.statusBar().showMessage("Adding Pad - Esc to stop")

    def _show_log_path(self) -> None:
        """Display the diagnostic log path for bug reports."""
        path = app_support._LOG_PATH or app_support._configure_logging()
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
        view_state = self._view_state_for_side(side)
        if view_state is not None:
            self._schedule_pad_refresh(view_state)
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
        view_state = self._active_views()[0].view_state()
        pad = self._pad_at(side, x, y)
        if pad and pad.pad_id == self._selected_pad_id:
            self._selected_net = None
            self._selected_pad_id = None
            self._trace_highlight_ids = None
        else:
            self._selected_net = pad.net if pad else None
            self._selected_pad_id = pad.pad_id if pad else None
            self._trace_highlight_ids = None
        self._schematic_view.set_selected_net(self._selected_net)
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
        """Collect one Shift-clicked board pad for the current connection group."""
        if getattr(self, "_nc_mode", False):
            pad = self._pad_at(side, x, y)
            if pad is not None:
                self._assign_pad_net(pad.pad_id, NC_NET)
            return
        if not self._connection_mode:
            return
        pad = self._pad_at(side, x, y)
        if pad is None:
            return
        self._append_connection_terminal(self._pad_terminal(pad))
        if self._pending_connection_terminals:
            view = self._views.get(side) or self._side_views.get(side)
            if view is not None:
                view.set_connection_preview_origin(x, y)

    @staticmethod
    def _pad_terminal(pad: Pad) -> tuple[str, str, str | None]:
        """Convert a physical pad into its schematic terminal identity."""
        if pad.device_id and pad.number:
            return "pin", pad.device_id, pad.number
        return "pad", pad.pad_id, None

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
        button_labels = {"both": "ALL", "image": "IMG", "pads": "PAD"}
        self._show_pads_button.setText(button_labels[self._pad_display_mode])
        label = labels[self._pad_display_mode]
        self._show_pads_button.setToolTip("Show/Hide Layers")
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
        active_views = self._active_views()
        state = active_views[0].view_state() if active_views else None
        self._refresh_views()
        self._tabs.setCurrentIndex(current_tab)
        if state is not None:
            self._apply_active_view_state(state)

    def _apply_active_view_state(self, state: tuple[float, float, float]) -> None:
        """Restore zoom/pan exactly; content mutations must never move the view."""
        self._pending_board_view_sync_source = None
        self._board_view_sync_pending = False
        self._view_restore_revision += 1
        revision = self._view_restore_revision
        self._apply_active_view_state_now(state)
        QTimer.singleShot(
            0,
            lambda: self._finish_active_view_state_restore(revision, state),
        )

    def _apply_active_view_state_now(self, state: tuple[float, float, float]) -> None:
        """Apply one viewport state without recapturing a quantized dual view."""
        active_views = tuple(self._active_views())
        self._syncing_views = True
        try:
            for view in active_views:
                view.apply_view_state(state)
            for view in (*self._views.values(), *self._side_views.values()):
                if view not in active_views:
                    view.defer_view_state(state)
        finally:
            self._syncing_views = False

    def _finish_active_view_state_restore(
        self, revision: int, state: tuple[float, float, float]
    ) -> None:
        """Correct scrollbar pan after Qt completes the refreshed-view layout."""
        if revision == self._view_restore_revision:
            self._apply_active_view_state_now(state)

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
            (
                replace(item, net=net)
                if item.pad_id == pad_id
                or (
                    pad.device_id is not None
                    and item.device_id == pad.device_id
                    and item.number == pad.number
                )
                else item
            )
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
        removed_nets = (
            self._cleanup_single_terminal_nets({pad.net})
            if pad.net is not None and pad.net != net
            else set()
        )
        keep_active_selection = (
            net is None
            and active_net == pad.net
            and active_pad_id is not None
            and active_pad_id != pad_id
            and active_net not in removed_nets
        )
        if is_nc_net(net):
            self._selected_net = None
            self._selected_pad_id = None
        elif removed_nets:
            self._selected_net = None
            self._selected_pad_id = None
        elif keep_active_selection:
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
            delete_action.triggered.connect(
                lambda: self._delete_pad(pad.pad_id, self._view_state_for_side(side))
            )
        else:
            delete_action = menu.addAction("Delete device")
            rotate_action = menu.addAction("Rotate 45°")
            swap_action = None
            if self._device_supports_pin_swap(device):
                swap_action = menu.addAction("Swap pins")
            component_action = menu.addAction("Edit Name…")
            value_action = menu.addAction("Set Value…")
            symbol_action = menu.addAction("Assign KiCad Component…")
            clear_symbol_action = None
            if device.symbol_name:
                clear_symbol_action = menu.addAction("Clear KiCad Component")
            description_action = menu.addAction("Edit description…")
            datasheet_action = menu.addAction("Edit datasheet…")
            delete_action.triggered.connect(
                lambda: self._delete_device(
                    device.device_id, self._view_state_for_side(side)
                )
            )
            rotate_action.triggered.connect(
                lambda: self._rotate_device(device.device_id)
            )
            if swap_action is not None:
                swap_action.triggered.connect(
                    lambda: self._swap_device_pins(device.device_id)
                )
            value_action.triggered.connect(
                lambda: self._edit_device_value(device.device_id)
            )
            symbol_action.triggered.connect(
                lambda: self._assign_kicad_component(device.device_id)
            )
            if clear_symbol_action is not None:
                clear_symbol_action.triggered.connect(
                    lambda: self._clear_kicad_component(device.device_id)
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
                pin_action = menu.addAction("Edit PIN…")
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

    def _device_supports_pin_swap(self, device: Device) -> bool:
        """Return whether a device source contains exactly two electrical pads."""
        footprint = self._footprint_for_device(device)
        return footprint is not None and footprint.pad_count() == 2

    def _swap_device_pins(self, device_id: str) -> None:
        """Rotate a two-pin device and exchange its electrical assignments."""
        if not self.project:
            return
        device = next(
            (item for item in self.project.devices if item.device_id == device_id),
            None,
        )
        if device is None or not self._device_supports_pin_swap(device):
            return
        pads = [pad for pad in self.project.pads if pad.device_id == device_id]
        try:
            updated_device, updated_pads = swap_two_pin_assignments(device, pads)
        except ValueError:
            LOGGER.warning("Cannot swap incomplete two-pin device=%s", device_id)
            return
        current_tab = self._tabs.currentIndex()
        views = self._active_views()
        view_state = views[0].view_state() if views else None
        pad_by_id = {pad.pad_id: pad for pad in updated_pads}
        self.project.devices = [
            updated_device if item.device_id == device_id else item
            for item in self.project.devices
        ]
        self.project.pads = [
            pad_by_id.get(item.pad_id, item) for item in self.project.pads
        ]
        self._rebuild_device_pads(device.side, self._base_pixmap_for_asset(device.side))
        self._dirty = True
        self._refresh_views()
        self._schematic_view.set_project(self.project)
        self._tabs.setCurrentIndex(current_tab)
        if view_state is not None:
            self._apply_active_view_state(view_state)
        self._update_title()
        LOGGER.info(
            "Swapped two-pin device id=%s reference=%s", device_id, device.reference
        )

    def _rotate_device(self, device_id: str) -> None:
        """Rotate one placed footprint clockwise by 45 degrees."""
        if not self.project:
            return
        device = next(
            (item for item in self.project.devices if item.device_id == device_id),
            None,
        )
        if device is None:
            return
        current_tab = self._tabs.currentIndex()
        views = self._active_views()
        view_state = views[0].view_state() if views else None
        rotation = (device.rotation + 45.0) % 360.0
        self.project.devices = [
            replace(item, rotation=rotation) if item.device_id == device_id else item
            for item in self.project.devices
        ]
        self._rebuild_device_pads(device.side, self._base_pixmap_for_asset(device.side))
        self._dirty = True
        self._refresh_views()
        self._tabs.setCurrentIndex(current_tab)
        if view_state is not None:
            self._apply_active_view_state(view_state)
        self._update_title()
        LOGGER.info(
            "Device rotated id=%s reference=%s rotation=%s",
            device_id,
            device.reference,
            rotation,
        )

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
        removed_nets = self._cleanup_single_terminal_nets({net})
        remaining = self._logical_net_terminals(net)
        self._selected_net = net if remaining and net not in removed_nets else None
        self._trace_highlight_ids = None
        if self._selected_net is None:
            self._selected_pad_id = None
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
        self._refresh_net_table()
        self._schematic_view.set_project(self.project)
        self._update_title()

    def _pad_menu_closed(self, menu: QMenu) -> None:
        """Release a closed asynchronous pad menu."""
        if self._pad_menu is menu:
            self._pad_menu = None

    def _delete_pad(
        self,
        pad_id: str,
        view_state: tuple[float, float, float] | None = None,
    ) -> None:
        """Delete one pad while preserving the action-source viewport."""
        if not self.project:
            return
        pad = next((item for item in self.project.pads if item.pad_id == pad_id), None)
        if pad is None:
            return
        view_state = view_state or self._view_state_for_side(pad.side)
        current_tab = self._tabs.currentIndex()
        self.project.pads = [
            item for item in self.project.pads if item.pad_id != pad_id
        ]
        self._cleanup_single_terminal_nets({pad.net} if pad.net else set())
        if self._selected_pad_id == pad_id:
            self._selected_pad_id = None
            self._selected_net = None
        self._dirty = True
        LOGGER.info("Pad deleted id=%s pad=%s", pad.pad_id, pad.name)
        self._refresh_views()
        self._tabs.setCurrentIndex(current_tab)
        if view_state is not None:
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
            "Edit name",
            f"Name/reference for {device.reference}:",
            text=device.reference,
        )
        if accepted:
            self._rename_device(device_id, reference)

    def _assign_kicad_component(self, device_id: str) -> None:
        """Download/select a compatible KiCad symbol and map its pins."""
        if not self.project:
            return
        device = next(
            (item for item in self.project.devices if item.device_id == device_id),
            None,
        )
        if device is None:
            return
        try:
            cache = KiCadSymbolCache(
                self._footprint_cache.root.parent / "kicad-symbols"
            )
            cache.ensure_ready()
            candidates = []
            pad_numbers = {
                pad.number for pad in self.project.pads if pad.device_id == device_id
            }
            for reference in cache.catalog():
                for symbol in parse_symbol_library(
                    reference.path.read_bytes(), reference.library
                ):
                    if len(symbol.pins) == len(pad_numbers):
                        candidates.append(symbol)
            if not candidates:
                QMessageBox.information(
                    self, "KiCad component", "No compatible symbol was found."
                )
                return
        except (OSError, KiCadCacheError, KiCadFormatError) as error:
            QMessageBox.warning(self, "KiCad component", str(error))
            return
        labels = [
            f"{symbol.name} — {symbol.library} ({len(symbol.pins)} pins)"
            for symbol in candidates
        ]
        selected, accepted = QInputDialog.getItem(
            self, "Select KiCad component", "Component:", labels, 0, True
        )
        if not accepted:
            return
        selected_index = next(
            (index for index, label in enumerate(labels) if label == selected), None
        )
        if selected_index is None:
            return
        symbol = candidates[selected_index]
        old_pins = {pin.number: pin for pin in device.pins}
        pins = [
            ComponentPin(
                pin.number,
                pin.name,
                pin.name,
                pin.number,
                (
                    old_pins[pin.number].net_id
                    if pin.number in old_pins
                    else None
                ),
            )
            for pin in symbol.pins
        ]
        self.project.devices = [
            replace(
                item,
                pins=pins,
                symbol_library_path=str(
                    next(
                        reference.path
                        for reference in cache.catalog()
                        if reference.library == symbol.library
                    )
                ),
                symbol_name=symbol.name,
            )
            if item.device_id == device_id
            else item
            for item in self.project.devices
        ]
        self._dirty = True
        self._refresh_views()
        self._update_title()

    def _clear_kicad_component(self, device_id: str) -> None:
        """Remove a KiCad symbol association while preserving footprint pins."""
        if not self.project:
            return
        self.project.devices = [
            replace(item, symbol_library_path=None, symbol_name=None)
            if item.device_id == device_id
            else item
            for item in self.project.devices
        ]
        self._dirty = True
        self._refresh_views()
        self._update_title()

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

    def _delete_device(
        self,
        device_id: str,
        view_state: tuple[float, float, float] | None = None,
    ) -> None:
        """Delete a footprint and pads without ever changing tab, zoom, or pan."""
        if not self.project:
            return
        device = next(
            (item for item in self.project.devices if item.device_id == device_id),
            None,
        )
        if device is None:
            return
        current_tab = self._tabs.currentIndex()
        view_state = view_state or self._view_state_for_side(device.side)
        removed_pad_ids = {
            pad.pad_id for pad in self.project.pads if pad.device_id == device_id
        }
        affected_nets = {
            net
            for net in (
                [pad.net for pad in self.project.pads if pad.device_id == device_id]
                + [pin.net_id for pin in device.pins]
            )
            if net
        }
        self.project.devices = [
            item for item in self.project.devices if item.device_id != device_id
        ]
        self.project.pads = [
            pad for pad in self.project.pads if pad.device_id != device_id
        ]
        self._cleanup_single_terminal_nets(affected_nets)
        still_used = any(
            item.footprint_definition_id == device.footprint_definition_id
            for item in self.project.devices
            if item.device_id != device_id
        )
        if self.store is not None and not still_used:
            self.store.remove_asset(device.footprint_path)
            self.project.footprint_definitions = [
                item
                for item in self.project.footprint_definitions
                if item.definition_id != device.footprint_definition_id
            ]
            self._device_footprint_cache.pop(
                device.footprint_definition_id or device.footprint_path, None
            )
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
        if view_state is not None:
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
