"""Board tabs, toolbar, view controls, and generic interaction."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,invalid-name,unused-import
# pylint: disable=too-few-public-methods,too-many-instance-attributes,duplicate-code
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
    QFont,
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


class BoardControlsMixin:
    """Provide boardcontrols behavior to the main window."""

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

        actual_button = add_button(
            "1:1",
            QStyle.StandardPixmap.SP_ComputerIcon,
            "1:1 Size",
            self._actual_size,
        )
        actual_button.setIcon(QIcon())
        actual_button.setText("1:1")
        actual_button.setFixedSize(40, 36)
        fit_button = add_button(
            "FIT",
            QStyle.StandardPixmap.SP_DesktopIcon,
            "Fit",
            self._fit_images,
        )
        rotate_button = add_button(
            "Rotate 90°",
            QStyle.StandardPixmap.SP_BrowserReload,
            "Rotate Board",
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
            "Measure Tool",
            self._toggle_ruler,
        )
        ruler_button.setIcon(QIcon())
        ruler_button.setText("📐")
        ruler_button.setFont(QFont(".AppleSystemUIFont", 20))
        ruler_button.setCheckable(True)
        self._ruler_button = ruler_button
        show_pads_button = QPushButton(panel)
        show_pads_button.setAccessibleName("Pad display mode")
        show_pads_button.setObjectName("toolShowPads")
        show_pads_button.setFixedSize(40, 36)
        show_pads_button.setText("ALL")
        show_pads_button.setToolTip("Show/Hide Layers")
        show_pads_button.setStatusTip(show_pads_button.toolTip())
        show_pads_button.clicked.connect(self._cycle_pad_display_mode)
        self._show_pads_button = show_pads_button
        layout.addWidget(show_pads_button)
        layout.addSpacing(12)
        info_button = add_button(
            "Info",
            QStyle.StandardPixmap.SP_FileDialogInfoView,
            "Component or pad information",
            self._show_info,
        )
        self._info_button = info_button
        device_button = add_button(
            "Add Footprint",
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
            "Add Component",
            self.add_device,
        )
        device_button.setIcon(
            QIcon.fromTheme(
                "list-add",
                self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder),
            )
        )
        device_button.setCheckable(True)
        self._add_device_button = device_button
        pad_button = add_button(
            "Add Pad",
            QStyle.StandardPixmap.SP_FileDialogNewFolder,
            "Add a Pad",
            self.create_pad,
        )
        pad_button.setIcon(_pad_tool_icon())
        pad_button.setCheckable(True)
        self._add_pad_button = pad_button
        move_button = add_button(
            "Move",
            QStyle.StandardPixmap.SP_ArrowForward,
            "Move footprint: click for Dual face, click again for Single face",
            self._cycle_move_mode,
        )
        move_button.setIcon(QIcon())
        move_button.setText("↔️")
        move_button.setFont(QFont(".AppleSystemUIFont", 18))
        move_button.setCheckable(True)
        self._move_button = move_button
        connect_button = add_button(
            "Connect",
            QStyle.StandardPixmap.SP_DialogApplyButton,
            "Connection mode: Shift+click pads or pins",
            self._toggle_connection_mode,
        )
        connect_button.setIcon(QIcon())
        connect_button.setText("--")
        connect_button.setCheckable(True)
        self._connect_button = connect_button
        nc_button = add_button(
            "NC",
            QStyle.StandardPixmap.SP_DialogCancelButton,
            "Set Pin not connected",
            self._toggle_nc_mode,
        )
        nc_button.setCheckable(True)
        nc_button.setIcon(QIcon())
        nc_button.setText("⛓️‍💥")
        nc_button.setFont(QFont(".AppleSystemUIFont", 18))
        self._nc_button = nc_button
        optimize_button = add_button(
            "Optimize Schematic",
            QStyle.StandardPixmap.SP_BrowserReload,
            "Optimize schematic layout",
            self._optimize_schematic,
        )
        self._optimize_schematic_button = optimize_button
        delete_button = add_button(
            "Delete",
            QStyle.StandardPixmap.SP_TrashIcon,
            "Delete Component",
            self._toggle_delete_mode,
        )
        delete_button.setCheckable(True)
        self._delete_button = delete_button
        layout.addStretch()
        image_button = add_button(
            "Image",
            QStyle.StandardPixmap.SP_DialogOpenButton,
            "Choose Top or Bottom image: load, resize, or remove",
            self.manage_picture,
        )
        image_button.setIcon(QIcon())
        image_button.setText("📷")
        image_button.setFont(QFont(".AppleSystemUIFont", 18))
        add_button(
            "Log file",
            QStyle.StandardPixmap.SP_FileDialogInfoView,
            "Show diagnostic log file path",
            self._show_log_path,
        )
        config_button = add_button(
            "Configuration",
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
            "Configure application display colors",
            self._show_config,
        )
        config_button.setIcon(
            QIcon.fromTheme(
                "preferences-system",
                self.style().standardIcon(
                    QStyle.StandardPixmap.SP_FileDialogDetailedView
                ),
            )
        )
        if config_button.icon().isNull():
            config_button.setText("⚙")
            config_button.setFont(QFont(".AppleSystemUIFont", 18))
        self._config_button = config_button
        save_button = add_button(
            "Save",
            QStyle.StandardPixmap.SP_DialogSaveButton,
            "Save project",
            self.save_project,
        )
        save_button.setIcon(_save_tool_icon())
        undo_button = add_button(
            "Undo",
            QStyle.StandardPixmap.SP_ArrowBack,
            "Undo the last action",
            self.undo,
        )
        redo_button = add_button(
            "Redo",
            QStyle.StandardPixmap.SP_ArrowForward,
            "Redo the last undone action",
            self.redo,
        )
        self._undo_button = undo_button
        self._redo_button = redo_button
        self._update_history_buttons()
        add_button(
            "Quit",
            QStyle.StandardPixmap.SP_DialogCloseButton,
            "Quit Tnasrevner",
            self.close,
        )
        panel.setStyleSheet(
            "QPushButton { padding: 6px 8px; }"
            "QPushButton:checked { background: #b84a4a; color: white; }"
            "QPushButton:hover { background: palette(highlight); "
            "color: palette(highlighted-text); }"
        )
        panel.setLayout(layout)
        dock.setWidget(panel)
        self._tools_dock = dock
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self._view_tool_buttons = (
            rotate_button,
            center_button,
            ruler_button,
            show_pads_button,
            device_button,
            pad_button,
            move_button,
            info_button,
            connect_button,
            nc_button,
            delete_button,
        )
        self._actual_button = actual_button
        self._fit_button = fit_button
        self._update_view_tools(self._tabs.currentIndex())

    def _update_view_tools(self, tab_index: int) -> None:
        """Enable only controls meaningful for active board or schematic view."""
        non_image = tab_index >= _CONNECTIONS_TAB
        for button in getattr(self, "_view_tool_buttons", ()):
            button.setEnabled(not non_image)
        if hasattr(self, "_actual_button"):
            self._actual_button.setEnabled(True)
        if hasattr(self, "_fit_button"):
            self._fit_button.setEnabled(True)
        if hasattr(self, "_optimize_schematic_button"):
            self._optimize_schematic_button.setVisible(tab_index == _SCHEMATIC_TAB)
            self._optimize_schematic_button.setEnabled(tab_index == _SCHEMATIC_TAB)
        if hasattr(self, "_move_button"):
            self._move_button.setEnabled(tab_index in (0, 1, 2))

    def _optimize_schematic(self) -> None:
        """Run the explicit optimizer with a live non-blocking progress dialog."""
        if (
            self._tabs.currentIndex() != _SCHEMATIC_TAB
            or not self.project
            or getattr(self, "_optimization_progress", None) is not None
        ):
            return
        self._schematic_optimization_viewport = self._schematic_view.view_state()
        self._capture_schematic_viewport()
        progress = QProgressDialog(
            "Optimizing schematic layout…",
            "Cancel",
            0,
            100,
            self,
        )
        progress.setWindowTitle("Schematic optimization")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setValue(0)
        progress.canceled.connect(self._schematic_view.cancel_optimization)
        self._optimization_progress = progress
        self._schematic_view.optimization_progress.connect(progress.setValue)
        self._schematic_view.optimization_finished.connect(
            self._finish_schematic_optimization
        )
        progress.show()
        self._schematic_view.optimize_layout()

    def _finish_schematic_optimization(self) -> None:
        """Close and release the schematic optimization progress dialog."""
        progress = getattr(self, "_optimization_progress", None)
        if progress is None:
            return
        self._restore_schematic_optimization_viewport()
        progress.close()
        progress.deleteLater()
        self._optimization_progress = None

    def _restore_schematic_optimization_viewport(self) -> None:
        """Restore schematic viewport captured before optimization started."""
        state = self._schematic_optimization_viewport
        if state is None:
            return
        self._schematic_view.apply_view_state(state)
        self._schematic_optimization_viewport = None
        self._capture_schematic_viewport()

    def _rotate_board_90(self) -> None:
        """Rotate both board images and all placed geometry by 90 degrees."""
        self._set_move_mode(None)
        self._exit_connection_mode()
        if not self.project or not self.store or not self.project.images:
            return

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
            if asset.path == asset.original_path and asset.original_path:
                transformations = asset.transformations + (
                    (90.0, (0.0, 0.0, 1.0, 1.0)),
                )
                rotated_images.append(
                    replace(
                        asset,
                        calibration_line=line,
                        transformations=transformations,
                    )
                )
                continue
            pixmap = self._base_pixmap_for_asset(asset.side)
            if not pixmap.isNull():
                rotated = pixmap.transformed(QTransform().rotate(90))
                self.store.write_asset(asset.path, self._pixmap_bytes(rotated))
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
        """Set global terminal-selection mode and cursor."""
        if enabled:
            self._set_move_mode(None)
            self._set_delete_mode(False)
            self._set_nc_mode(False)
            self._disable_ruler()
        self._connection_mode = enabled
        if not enabled:
            self._pending_connection_terminals.clear()
            self._connection_trace_pairs = None
            self._trace_highlight_ids = None
        button = getattr(self, "_connect_button", None)
        if button is not None:
            button.blockSignals(True)
            button.setChecked(enabled)
            button.blockSignals(False)
        for view in (*self._views.values(), *self._side_views.values()):
            view.set_connection_mode(enabled)
        self._schematic_view.set_connection_mode(enabled)
        if enabled:
            self._show_connection_prompt()

    def _set_nc_mode(self, enabled: bool) -> None:
        """Enable or disable intentional non-connection annotation mode.

        Args:
            enabled: Whether Shift-click should assign ``NC``.
        """
        if enabled:
            self._set_move_mode(None)
            self._set_delete_mode(False)
            self._set_connection_mode(False)
            self._disable_ruler()
        self._nc_mode = enabled
        button = getattr(self, "_nc_button", None)
        if button is not None:
            button.blockSignals(True)
            button.setChecked(enabled)
            button.blockSignals(False)
        if enabled:
            self._exit_connection_mode()
            self.statusBar().showMessage("NC mode: Shift+click pads or pins to mark NC")

    def _toggle_nc_mode(self) -> None:
        """Toggle intentional non-connection annotation mode."""
        self._set_nc_mode(not self._nc_mode)

    def _show_connection_prompt(self) -> None:
        """Display the persistent guidance for Connect mode."""
        self.statusBar().showMessage("Connection Mode, Shift to connect, Esc to quit")

    def _exit_connection_mode(self) -> None:
        """Leave Connect mode without creating a partial connection."""
        if not self._connection_mode:
            return
        self._set_connection_mode(False)
        self.statusBar().clearMessage()

    def _toggle_connection_mode(self) -> None:
        """Toggle multi-terminal connection selection."""
        if self._connection_mode:
            self._exit_connection_mode()
            return
        self._cancel_pad_placement()
        self._cancel_device_placement()
        self._set_delete_mode(False)
        self._disable_ruler()
        self._set_connection_mode(True)

    def _append_connection_terminal(self, value: object) -> None:
        """Commit a rolling terminal link and retain its new endpoint."""
        if not self._connection_mode or not self.project:
            return
        terminal = self._validated_terminal(value)
        if terminal is None or terminal in self._pending_connection_terminals:
            return
        if self._pending_connection_terminals:
            previous = self._pending_connection_terminals[-1]
            first_pad = self._connection_pad_id(previous)
            second_pad = self._connection_pad_id(terminal)
            if first_pad is not None and second_pad is not None:
                pairs = self._connection_trace_pairs or ()
                self._connection_trace_pairs = (*pairs, (first_pad, second_pad))
            self._connect_terminals((previous, terminal))
            self._pending_connection_terminals.clear()
        self._pending_connection_terminals.append(terminal)
        current_pad = self._connection_pad_id(terminal)
        selected_pad_ids = {
            pad_id for pair in self._connection_trace_pairs or () for pad_id in pair
        }
        if current_pad is not None:
            selected_pad_ids.add(current_pad)
        self._trace_highlight_ids = frozenset(selected_pad_ids)
        self._refresh_trace_selection_highlights()
        self._show_connection_prompt()

    def _refresh_trace_selection_highlights(self) -> None:
        """Refresh board rendering after transient endpoint changes."""
        for view in (*self._views.values(), *self._side_views.values()):
            view.set_trace_selection(
                self._selected_net,
                self._selected_pad_id,
                self._connection_trace_pairs,
                self._trace_highlight_ids,
            )

    def _finish_connection_selection(self) -> bool:
        """Stop the current rolling link preview when Shift is released."""
        if not self._connection_mode:
            return False
        self._pending_connection_terminals.clear()
        self._connection_trace_pairs = None
        self._trace_highlight_ids = None
        for view in (*self._views.values(), *self._side_views.values()):
            view.clear_connection_preview()
        self._schematic_view.clear_connection_preview()
        self._refresh_views_preserving_state()
        self._show_connection_prompt()
        return True

    def _connection_pad_id(self, terminal: tuple[str, str, str | None]) -> str | None:
        """Return generated board-pad ID for one logical connection terminal."""
        if not self.project:
            return None
        kind, object_id, number = terminal
        if kind == "pad":
            return object_id
        pad = next(
            (
                item
                for item in self.project.pads
                if item.device_id == object_id and item.number == number
            ),
            None,
        )
        return pad.pad_id if pad is not None else None

    def _show_info(self) -> None:
        """Show selected terminal info and exit any active connection mode."""
        self._set_move_mode(None)
        self._exit_connection_mode()
        if not self.project:
            QMessageBox.information(self, "Info", "No project loaded.")
            return
        if self._selected_schematic_terminal is not None:
            self._show_schematic_terminal_hover(self._selected_schematic_terminal)
            return
        if self._selected_pad_id is not None:
            pad = next(
                (
                    item
                    for item in self.project.pads
                    if item.pad_id == self._selected_pad_id
                ),
                None,
            )
            if pad is not None:
                self._show_pad_hover(pad)
                return
        self.statusBar().showMessage("Select a component, pad, or pin for info", 3000)

    def _toggle_delete_mode(self) -> None:
        """Toggle continuous deletion mode from the tools palette."""
        button = getattr(self, "_delete_button", None)
        self._set_delete_mode(button.isChecked() if button is not None else True)

    def _set_delete_mode(self, enabled: bool) -> None:
        """Enable or disable deletion mode on every board view."""
        if enabled:
            self._set_move_mode(None)
            self._exit_connection_mode()
            self._set_nc_mode(False)
            self._disable_ruler()
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
        """Delete an object without changing the clicked view zoom or pan."""
        if not self.project:
            return
        view_state = self._view_state_for_side(side)
        pad = self._pad_at(side, x, y)
        if pad is not None:
            if pad.device_id:
                self._delete_device(pad.device_id, view_state)
            else:
                self._delete_pad(pad.pad_id, view_state)
            if self._delete_button.isChecked():
                self.statusBar().showMessage("Deleting - Esc to stop")
            return
        device = self._device_at(side, x, y)
        if device is not None:
            self._delete_device(device.device_id, view_state)
        if self._delete_button.isChecked():
            self.statusBar().showMessage("Deleting - Esc to stop")

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
        self._set_move_mode(None)
        self._set_delete_mode(False)
        self._set_nc_mode(False)
        self._exit_connection_mode()
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
        if self._move_mode is not None:
            self._show_move_status()
            return
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
        value_text = f" | Value: {device.value}" if device and device.value else ""
        footprint_name = device.footprint_name if device is not None else "—"
        net_name = pad.net or "—"
        pin, _ = self._component_pin_for_pad(pad)
        function = (
            pin.function
            if pin is not None and pin.function
            else (
                self._default_pin_function(device, pin)
                if pin is not None
                else pad.function
            )
        ) or "—"
        pin_name = (
            f"{device.reference}.{pin.number}"
            if device is not None and pin is not None
            else "—"
        )
        self.statusBar().showMessage(
            f"Pad: {pad.name} | Device: {device_name}{value_text} | "
            f"Pin: {pin_name} | Footprint: {footprint_name} | "
            f"Function: {function} | Net: {net_name}"
        )

    def _show_schematic_terminal_hover(self, terminal: object) -> None:
        """Display pin or pad function information in the status bar."""
        if not isinstance(terminal, tuple) or len(terminal) != 3 or not self.project:
            if self._pending_pad is None and self._pending_device is None:
                self.statusBar().clearMessage()
            return
        kind, object_id, number = terminal
        if kind == "pad":
            pad = next(
                (item for item in self.project.pads if item.pad_id == object_id),
                None,
            )
            if pad is None:
                return
            pin, device = self._component_pin_for_pad(pad)
            function = (
                pin.function
                if pin is not None and pin.function
                else (
                    self._default_pin_function(device, pin)
                    if pin is not None
                    else pad.function
                )
            ) or "—"
            self.statusBar().showMessage(
                f"Pad: {pad.name} | Function: {function} | Net: {pad.net or '—'}"
            )
            return
        device = next(
            (item for item in self.project.devices if item.device_id == object_id),
            None,
        )
        if device is None or not isinstance(number, str):
            return
        pin = next((item for item in device.pins if item.number == number), None)
        function = self._default_pin_function(device, pin) if pin else "—"
        if pin is not None and pin.function:
            function = pin.function
        net = pin.net_id if pin is not None and pin.net_id else "—"
        self.statusBar().showMessage(
            f"Pin: {device.reference}.{number} | Function: {function} | Net: {net}"
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
        index = self._tabs.currentIndex()
        if index >= _CONNECTIONS_TAB:
            return []
        if index == 0:
            return [self._views["top"]]
        if index == 1:
            return [self._views["bottom"]]
        if index == 2:
            return list(self._side_views.values())
        return [self._overlay_view]

    def _view_state_for_side(self, side: str) -> tuple[float, float, float] | None:
        """Capture the action-source viewport, especially in the dual view."""
        if self._tabs.currentIndex() == 2 and side in self._side_views:
            return self._side_views[side].view_state()
        active = self._active_views()
        return active[0].view_state() if active else None

    def _sync_board_views(self, source: ImageView) -> None:
        """Synchronize zoom and pan between Top and Bottom views."""
        if self._syncing_views:
            return
        state = source.view_state()
        active_views = tuple(self._active_views())
        self._syncing_views = True
        try:
            for view in (*self._views.values(), *self._side_views.values()):
                if view is not source:
                    if view in active_views:
                        view.apply_view_state(state)
                    else:
                        view.defer_view_state(state)
        finally:
            self._syncing_views = False

    def _schedule_board_view_sync(self, source: ImageView) -> None:
        """Coalesce scrollbar and zoom signals into one board-view sync."""
        if self._syncing_views:
            return
        self._pending_board_view_sync_source = source
        if self._board_view_sync_pending:
            return
        self._board_view_sync_pending = True
        QTimer.singleShot(0, self._finish_board_view_sync)

    def _finish_board_view_sync(self) -> None:
        """Apply the newest coalesced board-view state."""
        source = self._pending_board_view_sync_source
        self._pending_board_view_sync_source = None
        self._board_view_sync_pending = False
        if source is not None:
            self._sync_board_views(source)

    def _show_zoom_ratio(self, ratio: float) -> None:
        """Display the active view zoom ratio in the information bar.

        Args:
            ratio: Zoom relative to the complete-image or overview scale.
        """
        if self._move_mode is not None:
            self._show_move_status()
            return
        self.statusBar().showMessage(f"Zoom: {ratio:.2f}x")

    def _actual_size(self) -> None:
        """Set active image view(s) to 1:1 scale."""
        if self._tabs.currentIndex() == _SCHEMATIC_TAB:
            self._schematic_view.actual_size()
            return
        for view in self._active_views():
            view.actual_size()

    def _fit_images(self) -> None:
        """Fit active image view(s) to their available space."""
        if self._tabs.currentIndex() == _SCHEMATIC_TAB:
            self._schematic_view.fit_overview()
            return
        for view in self._active_views():
            view.fit_image()

    def _center_images(self) -> None:
        """Center active image view(s)."""
        for view in self._active_views():
            view.center_image()
