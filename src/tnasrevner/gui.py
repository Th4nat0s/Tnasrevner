"""Basic desktop GUI for creating projects and displaying board pictures."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# Compatibility module intentionally re-exports public GUI classes.
# pylint: disable=no-name-in-module,invalid-name,unused-import,too-many-ancestors
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
    Net,
    Pad,
    ProjectDocument,
    ProjectFormatError,
    ProjectStore,
)


from .lib.app_support import (
    LOGGER,
    MeasurementSpinBox,
    ProjectDetailsDialog,
    StartupDialog,
    _application_data_directory,
    _application_icon,
    _configure_logging,
)
from .lib.board_controls import BoardControlsMixin
from .lib.bom_tab import BomTabMixin
from .lib.connections_tab import ConnectionsTabMixin
from .lib.footprint_actions import FootprintActionsMixin
from .lib.footprints import (
    FootprintPickerDialog,
    FootprintPreview,
    KiCadCacheWorker,
    PendingDevice,
    _paint_footprint,
)
from .lib.history_actions import HistoryActionsMixin
from .lib.image_editor import CropOverlay, ImageEditDialog
from .lib.image_import import ImageImportMixin
from .lib.image_view import ImageView
from .lib.pad_actions import PadActionsMixin
from .lib.project_io import ProjectIOMixin
from .lib.schematic_tab import SchematicCanvas, SchematicView
from .lib.app_config import AppConfig
from .lib.config_tab import ConfigPage


class MainWindow(
    BoardControlsMixin,
    FootprintActionsMixin,
    PadActionsMixin,
    HistoryActionsMixin,
    ProjectIOMixin,
    ImageImportMixin,
    ConnectionsTabMixin,
    BomTabMixin,
    QMainWindow,
):  # pylint: disable=too-many-instance-attributes,too-many-locals,too-many-statements
    """Minimal project and board-picture workspace."""

    def __init__(  # pylint: disable=too-many-statements
        self,
        show_startup: bool = True,
        footprint_cache: KiCadFootprintCache | None = None,
        settings: QSettings | None = None,
        config_path: Path | None = None,
    ) -> None:
        # Qt can dispatch application events while QMainWindow is initializing;
        # the event filter must therefore see a valid default immediately.
        self._connection_mode = False
        self._nc_mode = False
        super().__init__()
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)
        self.project: ProjectDocument | None = None
        self.store: ProjectStore | None = None
        self._dirty = False
        self._project_needs_save_as = False
        self._history: list[dict] = []
        self._history_index = -1
        self._history_backup_dirty = True
        self._history_restoring = False
        self._syncing_views = False
        self._board_view_sync_pending = False
        self._pending_board_view_sync_source: ImageView | None = None
        self._view_restore_revision = 0
        self._connection_mode = False
        self._pending_connection_terminals: list[tuple[str, str, str | None]] = []
        self._connection_trace_pairs: tuple[tuple[str, str], ...] | None = None
        self._pending_pad: Pad | None = None
        self._pending_device: PendingDevice | None = None
        self._add_device_pending = False
        self._selected_net: str | None = None
        self._selected_pad_id: str | None = None
        self._trace_highlight_ids: frozenset[str] | None = None
        self._selected_schematic_terminal: tuple[str, str, str | None] | None = None
        self._pads_visible = True
        self._pad_display_mode = "both"
        self._pad_refresh_pending = False
        self._pending_pad_view_state: tuple[float, float, float] | None = None
        self._schematic_optimization_viewport: tuple[float, int, int] | None = None
        self._image_cache: dict[str, QPixmap] = {}
        self._device_footprint_cache: dict[str, Footprint] = {}
        self._settings = (
            settings if settings is not None else QSettings("Tnasrevner", "Tnasrevner")
        )
        self._config = AppConfig.load(config_path or Path.cwd() / "config.yaml")
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
            "top": ImageView("No top picture", self._config),
            "bottom": ImageView("No bottom picture", self._config),
        }
        self._tabs = QTabWidget()
        self._tabs.addTab(self._views["top"], "Top")
        self._tabs.addTab(self._views["bottom"], "Bottom")
        side_by_side = QWidget()
        side_layout = QHBoxLayout(side_by_side)
        self._side_views = {
            "top": ImageView("No top picture", self._config),
            "bottom": ImageView("No bottom picture", self._config),
        }
        side_layout.addWidget(self._side_views["top"])
        side_layout.addWidget(self._side_views["bottom"])
        self._tabs.addTab(side_by_side, "Top + bottom")
        self._overlay_view = ImageView("No top/bottom images", self._config)
        self._tabs.addTab(self._overlay_view, "Both")
        self._net_table = QTableWidget(0, 6)
        self._net_table.setHorizontalHeaderLabels(
            ["Pad", "Net", "Pin", "Function", "Component", "Value"]
        )
        self._net_table.cellChanged.connect(self._net_table_cell_changed)
        self._tabs.addTab(self._net_table, "Connections")
        self._nets_table = QTableWidget(0, 2)
        self._nets_table.setHorizontalHeaderLabels(["NET Name", "Connections"])
        self._nets_table.cellChanged.connect(self._nets_table_cell_changed)
        self._tabs.addTab(self._nets_table, "NET")
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
        self._schematic_view = SchematicView(config=self._config)
        self._schematic_view.zoom_changed.connect(self._show_zoom_ratio)
        self._schematic_view.layout_changed.connect(self._schematic_layout_changed)
        self._schematic_view.layout_started.connect(self._schematic_layout_started)
        self._schematic_view.layout_finished.connect(self._schematic_layout_finished)
        self._schematic_view.layout_optimized.connect(self._schematic_layout_optimized)
        self._schematic_view.optimization_finished.connect(
            self._restore_schematic_viewport
        )
        self._schematic_view.terminal_selected.connect(self._select_schematic_terminal)
        self._schematic_view.terminal_hovered.connect(
            self._show_schematic_terminal_hover
        )
        self._schematic_view.terminal_net_edit_requested.connect(
            self._edit_schematic_terminal_net
        )
        self._schematic_view.terminal_menu_requested.connect(
            self._show_schematic_terminal_menu
        )
        self._schematic_view.device_context_requested.connect(
            self._show_schematic_device_menu
        )
        self._tabs.addTab(self._schematic_view, "Schematic")
        self._config_page = ConfigPage(self._config, self._configuration_changed)
        self._tabs.addTab(self._config_page, "Config")
        self._config_tab_index = self._tabs.indexOf(self._config_page)
        self._tabs.setTabVisible(self._config_tab_index, False)
        self._last_tab_index = self._tabs.currentIndex()
        self._tabs.currentChanged.connect(self._handle_tab_changed)
        for view in (*self._views.values(), *self._side_views.values()):
            view.zoom_changed.connect(self._show_zoom_ratio)
            view.view_changed.connect(
                lambda view=view: self._schedule_board_view_sync(view)
            )
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

    def _configuration_changed(self) -> None:
        """Refresh every board and schematic renderer after color changes."""
        for view in (
            *self._views.values(),
            *self._side_views.values(),
            self._overlay_view,
        ):
            view.set_config(self._config)
        self._schematic_view.set_config(self._config)

    def _show_config(self) -> None:
        """Open the application configuration tab."""
        self._tabs.setTabVisible(self._config_tab_index, True)
        self._tabs.setCurrentIndex(self._config_tab_index)


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
