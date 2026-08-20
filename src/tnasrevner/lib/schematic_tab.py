"""Borderless Schemdraw schematic tab and interaction."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,invalid-name,unused-import,too-many-lines
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
    is_nc_net,
)

# pylint: disable=unused-import

from .app_support import _footprint_family, _natural_sort_key, _reference_sort_key
from .app_config import (
    AppConfig,
    DEFAULT_COLORS,
    contrasting_text_color,
)
from .schematic_layout import SchematicOptimizationWorker
from .schematic_router import OrthogonalRouter


class SchematicCanvas(QWidget):  # pylint: disable=too-many-instance-attributes
    """Paint a borderless, zoomable electrical schematic."""

    # Large enough to feel unbounded, small enough for Qt scroll/layout math.
    _WORLD_SIZE = 20_000.0
    MIN_ZOOM = 0.05
    MAX_ZOOM = 10.0
    _SCHEMDRAW_UNIT_TO_SVG = 36.0
    _NET_LINE_WIDTH = 5.0
    _SELECTED_NET_LINE_WIDTH = 7.0
    _SCHEMATIC_FOREGROUND = "#e7edf5"
    _POWER_NETS = {
        "gnd": "GND",
        "v5": "V5",
        "5v": "V5",
        "v3.3": "V3.3",
        "3v3": "V3.3",
        "3.3v": "V3.3",
        "v12": "V12",
        "12v": "V12",
        "vbat": "VBAT",
        "vbatt": "VBAT",
    }

    layout_changed = Signal(str, float, float)
    layout_started = Signal(str)
    layout_finished = Signal(str)
    layout_optimized = Signal()
    optimization_progress = Signal(int)
    optimization_finished = Signal()
    terminal_selected = Signal(object)
    terminal_hovered = Signal(object)
    terminal_net_edit_requested = Signal(object)
    terminal_menu_requested = Signal(object)
    device_context_requested = Signal(str)
    pan_requested = Signal(int, int)

    def __init__(
        self, parent: QWidget | None = None, config: AppConfig | None = None
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._project: ProjectDocument | None = None
        self._project_dirty = False
        self._zoom = 1.0
        self._logical_width = 1400
        self._logical_height = 1100
        self._auto_centers: dict[str, QPointF] = {}
        self._device_centers: dict[str, QPointF] = {}
        self._pad_centers: dict[str, QPointF] = {}
        self._drag_device_id: str | None = None
        self._drag_pad_id: str | None = None
        self._drag_offset = QPointF()
        self._terminal_hits: list[tuple[QPointF, tuple[str, str, str | None]]] = []
        self._selected_net: str | None = None
        self._connection_preview_origin: QPointF | None = None
        self._connection_preview_cursor: QPointF | None = None
        self._optimization_thread: QThread | None = None
        self._optimization_worker: SchematicOptimizationWorker | None = None
        self._pan_position: QPoint | None = None
        self._schemdraw_cache: dict[
            tuple[str, tuple[str, ...], str],
            tuple[QSvgRenderer, QRectF, tuple[QPointF, ...]],
        ] = {}
        self._route_cache: dict[tuple, list[QPointF]] = {}
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(self._logical_width, self._logical_height)
        self.setAutoFillBackground(True)

    def set_config(self, config: AppConfig) -> None:
        """Use updated application colors and repaint the schematic.

        Args:
            config: Shared application display configuration.
        """
        self._config = config
        self.update()

    def _color(self, key: str) -> QColor:
        """Resolve a configured schematic color.

        Args:
            key: Semantic color key.

        Returns:
            Qt color for the requested rendering state.
        """
        value = (
            self._config.colors.get(key, DEFAULT_COLORS[key])
            if self._config
            else DEFAULT_COLORS[key]
        )
        return QColor(value)

    def set_project(self, project: ProjectDocument | None) -> None:
        """Replace the displayed project and repaint the schematic."""
        self._project = project
        self._route_cache.clear()
        self._pad_centers.clear()
        self._project_dirty = True
        if not self.isVisible():
            return
        self._refresh_project()

    def _refresh_project(self) -> None:
        """Apply pending project data only while canvas is visible."""
        self._auto_centers = self._layout_devices()
        self._resize_for_project()
        self._project_dirty = False
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        """Materialize deferred schematic rendering when its tab is opened."""
        super().showEvent(event)
        if self._project_dirty:
            self._refresh_project()

    def set_selected_net(self, net: str | None) -> None:
        """Highlight one selected electrical connection."""
        self._selected_net = net
        self.update()

    def set_connection_mode(self, enabled: bool) -> None:
        """Use crosshair cursor while editing schematic connections."""
        if enabled:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        else:
            self.unsetCursor()
            self.clear_connection_preview()

    def set_connection_preview_terminal(
        self, terminal: tuple[str, str, str | None]
    ) -> None:
        """Start a temporary line from a selected schematic terminal."""
        match = next(
            (
                point
                for point, candidate in self._terminal_hits
                if candidate == terminal
            ),
            None,
        )
        if match is None:
            return
        self._connection_preview_origin = match
        self._connection_preview_cursor = (
            self.mapFromGlobal(QCursor.pos()).toPointF() / self._zoom
        )
        self.update()

    def clear_connection_preview(self) -> None:
        """Remove the non-persistent schematic connection preview line."""
        if self._connection_preview_origin is None:
            return
        self._connection_preview_origin = None
        self._connection_preview_cursor = None
        self.update()

    def _resize_for_project(self) -> None:
        """Keep a large borderless world around compactly placed content."""
        self._logical_width = round(self._WORLD_SIZE)
        self._logical_height = round(self._WORLD_SIZE)
        self.resize(
            round(self._logical_width * self._zoom),
            round(self._logical_height * self._zoom),
        )

    def set_zoom(self, zoom: float) -> None:
        """Resize the canvas while keeping its schematic geometry vector-based."""
        self._zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, zoom))
        self.resize(
            round(self._logical_width * self._zoom),
            round(self._logical_height * self._zoom),
        )
        self.update()

    def zoom(self) -> float:
        """Return the current canvas zoom factor."""
        return self._zoom

    def fit_overview(self) -> None:
        """Fit the complete schematic sheet and center it in the viewport."""
        parent = self.parentWidget()
        viewport = parent.size() if parent is not None else self.size()
        bounds = self._content_bounds()
        width_ratio = viewport.width() / max(1.0, bounds.width())
        height_ratio = viewport.height() / max(1.0, bounds.height())
        self.set_zoom(
            max(self.MIN_ZOOM, min(self.MAX_ZOOM, width_ratio, height_ratio) * 0.82)
        )
        scroll_area = parent
        while scroll_area is not None and not hasattr(
            scroll_area, "horizontalScrollBar"
        ):
            scroll_area = scroll_area.parentWidget()
        if scroll_area is not None:
            scroll_area.horizontalScrollBar().setValue(
                round((bounds.center().x() * self._zoom) - viewport.width() / 2)
            )
            scroll_area.verticalScrollBar().setValue(
                round((bounds.center().y() * self._zoom) - viewport.height() / 2)
            )

    def _center_for_device(self, device: Device, index: int) -> QPointF:
        """Return persisted schematic position or a stable automatic position."""
        if device.schematic_x is not None and device.schematic_y is not None:
            return QPointF(device.schematic_x, device.schematic_y)
        del index
        return self._auto_centers.get(
            device.device_id,
            QPointF(self._WORLD_SIZE / 2, self._WORLD_SIZE / 2),
        )

    def _content_bounds(self) -> QRectF:
        """Return tight bounds around current symbols and independent pads."""
        if self._project is None:
            return QRectF(
                self._WORLD_SIZE / 2 - 240,
                self._WORLD_SIZE / 2 - 160,
                480,
                320,
            )
        bounds: QRectF | None = None
        for index, device in enumerate(self._project.devices):
            center = self._center_for_device(device, index)
            item = self._device_bounds(device, center)
            bounds = item if bounds is None else bounds.united(item)
        independent_pads = [pad for pad in self._project.pads if not pad.device_id]
        for index, _pad in enumerate(independent_pads):
            point = self._independent_pad_center(index, len(independent_pads), _pad)
            item = QRectF(point.x() - 30, point.y() - 30, 60, 60)
            bounds = item if bounds is None else bounds.united(item)
        if bounds is None:
            center = QPointF(self._WORLD_SIZE / 2, self._WORLD_SIZE / 2)
            return QRectF(center.x() - 240, center.y() - 160, 480, 320)
        return bounds.adjusted(-80, -80, 80, 80)

    def _independent_pad_center(
        self, index: int, count: int, pad: Pad | None = None
    ) -> QPointF:
        """Return persisted or perimeter position for one independent pad.

        Args:
            index: Pad index in automatic layout.
            count: Number of independent pads.
            pad: Pad whose persisted schematic position should be restored.

        Returns:
            Schematic center for the pad.
        """
        if pad is not None:
            if pad.schematic_x is not None:
                return QPointF(pad.schematic_x, pad.schematic_y)
            cached = self._pad_centers.get(pad.pad_id)
            if cached is not None:
                return cached
        bounds = self._content_bounds_without_pads()
        if pad is not None:
            width = max(1600.0, bounds.width())
            height = max(1200.0, bounds.height())
            center = bounds.center()
            left = center.x() - width / 2
            top = center.y() - height / 2
            margin = 220.0
            distances = (
                (pad.y, "top"),
                (1.0 - pad.x, "right"),
                (1.0 - pad.y, "bottom"),
                (pad.x, "left"),
            )
            side = min(distances, key=lambda item: (item[0], item[1]))[1]
            if side == "top":
                return QPointF(left + pad.x * width, top - margin)
            if side == "right":
                return QPointF(left + width + margin, top + pad.y * height)
            if side == "bottom":
                return QPointF(left + pad.x * width, top + height + margin)
            return QPointF(left - margin, top + pad.y * height)
        columns = max(1, min(8, count))
        return QPointF(
            bounds.center().x() + (index % columns - (columns - 1) / 2) * 180,
            bounds.bottom() + 180 + (index // columns) * 100,
        )

    def _pad_render_geometry(
        self,
        index: int,
        count: int,
        pad: Pad,
        device_net_points: dict[str, list[tuple[QPointF, QPointF]]],
        pad_only_centers: dict[str, QPointF] | None = None,
    ) -> tuple[QPointF, QPointF]:
        """Place one pad near its exact connected pin when available.

        Args:
            index: Pad index in automatic layout.
            count: Number of independent pads.
            pad: Pad being rendered.
            device_net_points: Rendered component terminals grouped by net.
            pad_only_centers: Compact centers for nets containing only pads.

        Returns:
            Pad center and outward routing direction.
        """
        group_key = (
            self._net_group_key(pad.net)
            if pad.net and not is_nc_net(pad.net)
            else None
        )
        terminals = device_net_points.get(group_key, []) if group_key else []
        if terminals:
            if pad.schematic_x is not None:
                center = QPointF(pad.schematic_x, pad.schematic_y)
                endpoint, _terminal_outward = min(
                    terminals,
                    key=lambda item: QLineF(center, item[0]).length(),
                )
                delta = endpoint - center
                if abs(delta.x()) >= abs(delta.y()):
                    outward = QPointF(1 if delta.x() >= 0 else -1, 0)
                else:
                    outward = QPointF(0, 1 if delta.y() >= 0 else -1)
                return center, outward
            peers = sorted(
                (
                    item
                    for item in self._project.pads
                    if item.device_id is None
                    and item.net
                    and self._net_group_key(item.net) == group_key
                ),
                key=lambda item: item.pad_id,
            )
            peer_index = next(
                (
                    peer_index
                    for peer_index, peer in enumerate(peers)
                    if peer.pad_id == pad.pad_id
                ),
                0,
            )
            endpoint, terminal_outward = terminals[peer_index % len(terminals)]
            layer = peer_index // len(terminals)
            normal = QPointF(-terminal_outward.y(), terminal_outward.x())
            lateral_slot = (
                0 if layer == 0 else ((layer + 1) // 2) * (-1 if layer % 2 else 1)
            )
            center = (
                endpoint
                + terminal_outward * (110.0 + abs(lateral_slot) * 60.0)
                + normal * lateral_slot * 70.0
            )
            return center, -terminal_outward
        compact_center = (pad_only_centers or {}).get(pad.pad_id)
        center = (
            compact_center
            if compact_center is not None
            else self._independent_pad_center(index, count, pad)
        )
        target = self._content_bounds_without_pads().center()
        delta = target - center
        if abs(delta.x()) >= abs(delta.y()):
            outward = QPointF(1 if delta.x() >= 0 else -1, 0)
        else:
            outward = QPointF(0, 1 if delta.y() >= 0 else -1)
        return center, outward

    def _pad_only_net_centers(
        self,
        device_net_points: dict[str, list[tuple[QPointF, QPointF]]],
    ) -> dict[str, QPointF]:
        """Compact automatic pads whose net has no component terminal.

        Args:
            device_net_points: Rendered component terminals grouped by net.

        Returns:
            Automatic pad centers indexed by pad ID.
        """
        if self._project is None:
            return {}
        grouped: dict[str, list[Pad]] = {}
        for pad in self._project.pads:
            if (
                pad.device_id is None
                and pad.net
                and not is_nc_net(pad.net)
                and pad.schematic_x is None
                and not device_net_points.get(self._net_group_key(pad.net))
            ):
                grouped.setdefault(self._net_group_key(pad.net), []).append(pad)
        grouped = {
            group_key: pads for group_key, pads in grouped.items() if len(pads) >= 2
        }
        if not grouped:
            return {}

        bounds = self._content_bounds_without_pads()
        side_groups: dict[str, list[tuple[float, str, list[Pad]]]] = {
            "top": [],
            "right": [],
            "bottom": [],
            "left": [],
        }
        for group_key, pads in grouped.items():
            average_x = sum(pad.x for pad in pads) / len(pads)
            average_y = sum(pad.y for pad in pads) / len(pads)
            side = min(
                (
                    (average_y, "top"),
                    (1.0 - average_x, "right"),
                    (1.0 - average_y, "bottom"),
                    (average_x, "left"),
                ),
                key=lambda item: (item[0], item[1]),
            )[1]
            tangent = (
                bounds.left() + average_x * bounds.width()
                if side in {"top", "bottom"}
                else bounds.top() + average_y * bounds.height()
            )
            side_groups[side].append((tangent, group_key, pads))

        centers: dict[str, QPointF] = {}
        spacing = 90.0
        group_gap = 120.0
        margin = 170.0
        for side, groups in side_groups.items():
            cursor = -math.inf
            for desired, _group_key, pads in sorted(groups):
                horizontal = side in {"top", "bottom"}
                if horizontal:
                    pads.sort(
                        key=lambda item: (
                            item.x,
                            item.name.casefold(),
                            item.pad_id,
                        )
                    )
                else:
                    pads.sort(
                        key=lambda item: (
                            item.y,
                            item.name.casefold(),
                            item.pad_id,
                        )
                    )
                half_span = (len(pads) - 1) * spacing / 2.0
                tangent = max(desired, cursor + group_gap + half_span)
                cursor = tangent + half_span
                for pad_index, pad in enumerate(pads):
                    offset = (pad_index - (len(pads) - 1) / 2.0) * spacing
                    if side == "top":
                        centers[pad.pad_id] = QPointF(
                            tangent + offset, bounds.top() - margin
                        )
                    elif side == "bottom":
                        centers[pad.pad_id] = QPointF(
                            tangent + offset, bounds.bottom() + margin
                        )
                    elif side == "left":
                        centers[pad.pad_id] = QPointF(
                            bounds.left() - margin, tangent + offset
                        )
                    else:
                        centers[pad.pad_id] = QPointF(
                            bounds.right() + margin, tangent + offset
                        )
        return centers

    @staticmethod
    def _minimum_spanning_pairs(points: list[QPointF]) -> list[tuple[int, int]]:
        """Connect net terminals with deterministic Manhattan minimum tree.

        Args:
            points: Terminal stub positions.

        Returns:
            Index pairs defining a short connected tree.
        """
        if len(points) < 2:
            return []
        visited = {0}
        pairs: list[tuple[int, int]] = []
        while len(visited) < len(points):
            candidates = []
            for left in sorted(visited):
                for right, _point in enumerate(points):
                    if right in visited:
                        continue
                    distance = abs(points[left].x() - points[right].x()) + abs(
                        points[left].y() - points[right].y()
                    )
                    candidates.append((distance, left, right))
            _distance, left, right = min(candidates)
            pairs.append((left, right))
            visited.add(right)
        return pairs

    def _content_bounds_without_pads(self) -> QRectF:
        """Return bounds around devices only, with a centered empty fallback."""
        if self._project is None or not self._project.devices:
            return QRectF(
                self._WORLD_SIZE / 2 - 240,
                self._WORLD_SIZE / 2 - 160,
                480,
                320,
            )
        bounds: QRectF | None = None
        for index, device in enumerate(self._project.devices):
            center = self._center_for_device(device, index)
            item = self._device_bounds(device, center)
            bounds = item if bounds is None else bounds.united(item)
        return bounds or QRectF(
            self._WORLD_SIZE / 2 - 240,
            self._WORLD_SIZE / 2 - 160,
            480,
            320,
        )

    def _layout_devices(self) -> dict[str, QPointF]:
        """Place new symbols with a compact deterministic force-directed layout."""
        if self._project is None:
            return {}
        devices = list(self._project.devices)
        movable = [
            device
            for device in devices
            if device.schematic_x is None or device.schematic_y is None
        ]
        if not movable:
            return {}
        locked = {
            device.device_id: QPointF(device.schematic_x, device.schematic_y)
            for device in devices
            if device.schematic_x is not None and device.schematic_y is not None
        }
        origin = QPointF(self._WORLD_SIZE / 2, self._WORLD_SIZE / 2)
        if locked:
            origin = QPointF(
                sum(point.x() for point in locked.values()) / len(locked),
                sum(point.y() for point in locked.values()) / len(locked),
            )
        positions: dict[str, QPointF] = dict(locked)
        for index, device in enumerate(movable):
            positions[device.device_id] = origin + QPointF(
                (index % 4 - 1.5) * 240,
                (index // 4 - (len(movable) - 1) / 8) * 190,
            )
        edges = self._layout_edges(devices)
        for _iteration in range(90):
            forces = {device.device_id: QPointF() for device in movable}
            for left_index, left in enumerate(devices):
                for right in devices[left_index + 1 :]:
                    delta = positions[right.device_id] - positions[left.device_id]
                    distance = max(1.0, math.hypot(delta.x(), delta.y()))
                    minimum = (
                        max(self._symbol_size(left)) / 2
                        + max(self._symbol_size(right)) / 2
                        + 55
                    )
                    repulsion = max(0.0, minimum - distance) * 0.11
                    vector = QPointF(delta.x() / distance, delta.y() / distance)
                    if left.device_id in forces:
                        forces[left.device_id] -= vector * repulsion
                    if right.device_id in forces:
                        forces[right.device_id] += vector * repulsion
            for left_id, right_id in edges:
                delta = positions[right_id] - positions[left_id]
                distance = max(1.0, math.hypot(delta.x(), delta.y()))
                vector = QPointF(delta.x() / distance, delta.y() / distance)
                attraction = (distance - 230) * 0.018
                if left_id in forces:
                    forces[left_id] += vector * attraction
                if right_id in forces:
                    forces[right_id] -= vector * attraction
            for device in movable:
                point = positions[device.device_id]
                force = forces[device.device_id]
                positions[device.device_id] = QPointF(
                    max(500, min(self._WORLD_SIZE - 500, point.x() + force.x())),
                    max(500, min(self._WORLD_SIZE - 500, point.y() + force.y())),
                )
        return {
            device_id: point
            for device_id, point in positions.items()
            if device_id not in locked
        }

    def _layout_edges(self, devices: list[Device]) -> set[tuple[str, str]]:
        """Build pairwise graph edges from shared logical nets."""
        by_net: dict[str, list[str]] = {}
        for device in devices:
            for pin in device.pins:
                if (
                    pin.net_id
                    and not is_nc_net(pin.net_id)
                    and self._power_net_label(pin.net_id) is None
                ):
                    by_net.setdefault(pin.net_id, []).append(device.device_id)
        edges: set[tuple[str, str]] = set()
        for members in by_net.values():
            unique = sorted(set(members))
            for index, left in enumerate(unique):
                for right in unique[index + 1 :]:
                    edges.add((left, right))
        return edges

    @classmethod
    def _layout_connections(
        cls, devices: list[Device]
    ) -> list[tuple[str, int, str, int]]:
        """Build deterministic pin-to-pin connections grouped by NET.

        Args:
            devices: Components whose pins should be inspected.

        Returns:
            Pairwise device and pin indexes for every shared NET.
        """
        by_net: dict[str, list[tuple[str, int]]] = {}
        for device in devices:
            for index, pin in enumerate(device.pins):
                if (
                    pin.net_id
                    and not is_nc_net(pin.net_id)
                    and cls._power_net_label(pin.net_id) is None
                ):
                    by_net.setdefault(pin.net_id, []).append((device.device_id, index))
        connections: list[tuple[str, int, str, int]] = []
        for members in by_net.values():
            unique = sorted(set(members))
            if len(unique) < 2:
                continue
            origin = unique[0]
            connections.extend(
                (origin[0], origin[1], target[0], target[1]) for target in unique[1:]
            )
        return connections

    def optimize_layout(self) -> None:
        """Start layout optimization in a worker thread with progress signals."""
        if (
            self._project is None
            or len(self._project.devices) < 2
            or self._optimization_thread is not None
        ):
            return
        positions = {
            device.device_id: self._center_for_device(device, index)
            for index, device in enumerate(self._project.devices)
        }
        rotations = {
            device.device_id: device.schematic_rotation
            for device in self._project.devices
        }
        worker = SchematicOptimizationWorker(
            self._project.devices,
            positions,
            rotations,
            self._layout_edges(self._project.devices),
            self._layout_connections(self._project.devices),
            self._WORLD_SIZE,
            self._symbol_size,
        )
        worker.progress.connect(self.optimization_progress)
        worker.completed.connect(self._apply_optimized_layout)
        worker.cancelled.connect(self._optimization_cancelled)
        worker.finished.connect(self._optimization_thread_finished)
        self._optimization_thread = worker
        self._optimization_worker = worker
        worker.start()

    @Slot(object, object)
    def _apply_optimized_layout(self, positions: object, rotations: object) -> None:
        """Apply worker results on the GUI thread and repaint the schematic."""
        if not isinstance(positions, dict) or not isinstance(rotations, dict):
            return
        if self._project is None:
            return
        self._project.devices = [
            replace(
                device,
                schematic_x=positions[device.device_id].x(),
                schematic_y=positions[device.device_id].y(),
                schematic_rotation=rotations[device.device_id],
            )
            for device in self._project.devices
        ]
        self._project.pads = [
            (
                replace(pad, schematic_x=None, schematic_y=None)
                if pad.device_id is None and not pad.schematic_glued
                else pad
            )
            for pad in self._project.pads
        ]
        self._device_centers = positions
        self._auto_centers = {}
        self._pad_centers.clear()
        self.update()
        self.layout_optimized.emit()
        self.optimization_finished.emit()

    @Slot()
    def _optimization_thread_finished(self) -> None:
        """Release worker references after the optimization thread exits."""
        self._optimization_thread = None
        self._optimization_worker = None

    @Slot()
    def _optimization_cancelled(self) -> None:
        """Notify the UI that optimization stopped without applying results."""
        self.optimization_finished.emit()

    def cancel_optimization(self) -> None:
        """Request cooperative cancellation of the active layout worker."""
        if self._optimization_worker is not None:
            self._optimization_worker.requestInterruption()

    @classmethod
    def _symbol_size(cls, device: Device) -> tuple[float, float]:
        """Return the collision/display size of one schematic symbol."""
        if cls._symbol_kind(device) == "uc":
            pins = max(1, len(device.pins))
            side_pins = max(1, math.ceil(pins / 4))
            side = max(160.0, side_pins * 36.0 + 56.0)
            return side, side
        if cls._symbol_kind(device) == "connector":
            return 150.0, max(100.0, len(device.pins) * 20.0 + 30.0)
        if cls._symbol_kind(device) == "transistor":
            return 150.0, 130.0
        if cls._symbol_kind(device) in {"resistor", "capacitor"}:
            return 110.0, 28.0
        return 150.0, 120.0

    @classmethod
    def _device_bounds(cls, device: Device, center: QPointF) -> QRectF:
        """Return a padded collision rectangle for one component."""
        width, height = cls._symbol_size(device)
        if round(device.schematic_rotation / 90.0) % 2:
            width, height = height, width
        padding = 4.0 if cls._symbol_kind(device) in {"resistor", "capacitor"} else 12.0
        return QRectF(
            center.x() - width / 2 - padding,
            center.y() - height / 2 - padding,
            width + padding * 2,
            height + padding * 2,
        )

    @staticmethod
    def _pin_annotation(device: Device, pin: ComponentPin) -> tuple[str, str]:
        """Return the complete pin reference and optional function line."""
        function = pin.function.strip()
        if function == f"Pin {pin.number}":
            function = ""
        return f"{device.reference}.{pin.number}", f"- {function}" if function else ""

    def _device_at(self, point: QPointF) -> str | None:
        """Return the component under a logical canvas point."""
        if self._project is None:
            return None
        devices = {device.device_id: device for device in self._project.devices}
        for device_id, center in reversed(tuple(self._device_centers.items())):
            device = devices.get(device_id)
            if device is not None and self._device_bounds(device, center).contains(
                point
            ):
                return device_id
        return None

    def _pad_at(self, point: QPointF) -> str | None:
        """Return independent schematic pad under a logical point."""
        if self._project is None:
            return None
        for pad in self._project.pads:
            if pad.device_id:
                continue
            center = self._pad_centers.get(pad.pad_id)
            if center is not None and QLineF(point, center).length() <= 32:
                return pad.pad_id
        return None

    def _terminal_at(
        self, point: QPointF
    ) -> tuple[tuple[str, str, str | None], QPointF] | None:
        """Return the nearest schematic terminal around a logical point."""
        candidates = [
            (QLineF(point, terminal_point).length(), terminal, terminal_point)
            for terminal_point, terminal in self._terminal_hits
            if QLineF(point, terminal_point).length() <= 28.0
        ]
        if not candidates:
            return None
        _distance, terminal, terminal_point = min(candidates, key=lambda item: item[0])
        return terminal, terminal_point

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Start dragging a schematic component."""
        point = event.position() / self._zoom
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            hit = self._terminal_at(point)
            if hit is None:
                return
            terminal, _terminal_point = hit
            self.terminal_selected.emit(terminal)
            self.setFocus()
            event.accept()
            self.update()
            return
        terminal_hit = self._terminal_at(point)
        pad_id = self._pad_at(point)
        if terminal_hit is None and pad_id is not None:
            terminal_hit = (
                ("pad", pad_id, None),
                self._pad_centers[pad_id],
            )
        if (
            event.button() == Qt.MouseButton.RightButton
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            and terminal_hit is not None
        ):
            terminal, _terminal_point = terminal_hit
            self.terminal_net_edit_requested.emit(terminal)
            event.accept()
            return
        if (
            event.button() == Qt.MouseButton.RightButton
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
            and terminal_hit is not None
        ):
            terminal, _terminal_point = terminal_hit
            self.terminal_menu_requested.emit(terminal)
            event.accept()
            return
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            if pad_id is not None:
                pad = next(
                    (item for item in self._project.pads if item.pad_id == pad_id),
                    None,
                )
                if pad is not None and not pad.schematic_glued:
                    self._drag_pad_id = pad_id
                    self._drag_offset = point - self._pad_centers[pad_id]
                    self.layout_started.emit(f"pad:{pad_id}")
                event.accept()
                return
            if terminal_hit is not None:
                if terminal_hit[0] == "pad":
                    pad_id = terminal_hit[1]
                    pad = next(
                        (item for item in self._project.pads if item.pad_id == pad_id),
                        None,
                    )
                    if pad is not None and not pad.schematic_glued:
                        self._drag_pad_id = pad_id
                        self._drag_offset = point - self._pad_centers[pad_id]
                        self.layout_started.emit(f"pad:{pad_id}")
                    event.accept()
                    return
                terminal, _terminal_point = terminal_hit
                self.terminal_selected.emit(terminal)
                event.accept()
                return
        device_id = self._device_at(point)
        if event.button() == Qt.MouseButton.RightButton and device_id is not None:
            self.device_context_requested.emit(device_id)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if device_id is None:
            self._pan_position = event.globalPosition().toPoint()
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            event.accept()
            return
        device = next(
            (item for item in self._project.devices if item.device_id == device_id),
            None,
        )
        if device is None or device.schematic_glued:
            event.accept()
            return
        self._drag_device_id = device_id
        self._drag_offset = point - self._device_centers[device_id]
        self.layout_started.emit(device_id)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        """Move the selected component on the schematic grid."""
        point = event.position() / self._zoom
        if self._connection_preview_origin is not None:
            self._connection_preview_cursor = point
            self.update()
        terminal_hit = self._terminal_at(point)
        self.terminal_hovered.emit(terminal_hit[0] if terminal_hit else None)
        if self._pan_position is not None:
            current = event.globalPosition().toPoint()
            delta = current - self._pan_position
            self._pan_position = current
            self.pan_requested.emit(delta.x(), delta.y())
            event.accept()
            return
        if self._drag_pad_id is not None and self._project is not None:
            point = event.position() / self._zoom - self._drag_offset
            point.setX(max(32.0, min(self._logical_width - 32.0, point.x())))
            point.setY(max(32.0, min(self._logical_height - 32.0, point.y())))
            self._pad_centers[self._drag_pad_id] = point
            self._project.pads = [
                (
                    replace(
                        pad,
                        schematic_x=point.x(),
                        schematic_y=point.y(),
                    )
                    if pad.pad_id == self._drag_pad_id
                    else pad
                )
                for pad in self._project.pads
            ]
            self.update()
            self.layout_changed.emit(f"pad:{self._drag_pad_id}", point.x(), point.y())
            event.accept()
            return
        if self._drag_device_id is None or self._project is None:
            if self._device_at(point) is not None:
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.unsetCursor()
            return
        self.setCursor(Qt.CursorShape.CrossCursor)
        point = event.position() / self._zoom - self._drag_offset
        device = next(
            (
                item
                for item in self._project.devices
                if item.device_id == self._drag_device_id
            ),
            None,
        )
        if device is None:
            return
        width, height = self._symbol_size(device)
        point.setX(
            max(width / 2 + 12, min(self._logical_width - width / 2 - 12, point.x()))
        )
        point.setY(
            max(height / 2 + 12, min(self._logical_height - height / 2 - 12, point.y()))
        )
        for other in self._project.devices:
            if other.device_id == device.device_id:
                continue
            other_center = self._device_centers.get(other.device_id)
            if other_center is not None and self._device_bounds(
                other, other_center
            ).intersects(self._device_bounds(device, point)):
                return
        self._device_centers[self._drag_device_id] = point
        self._project.devices = [
            (
                replace(device, schematic_x=point.x(), schematic_y=point.y())
                if device.device_id == self._drag_device_id
                else device
            )
            for device in self._project.devices
        ]
        self.update()
        self.layout_changed.emit(self._drag_device_id, point.x(), point.y())
        event.accept()

    def leaveEvent(self, event) -> None:  # noqa: N802
        """Clear schematic terminal hover information when leaving the canvas."""
        self.terminal_hovered.emit(None)
        self.clear_connection_preview()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """Finish component dragging."""
        dragged_device_id = self._drag_device_id
        dragged_pad_id = self._drag_pad_id
        self._drag_device_id = None
        self._drag_pad_id = None
        self._pan_position = None
        self.unsetCursor()
        if dragged_device_id is not None:
            self.layout_finished.emit(dragged_device_id)
        if dragged_pad_id is not None:
            self.layout_finished.emit(f"pad:{dragged_pad_id}")
        super().mouseReleaseEvent(event)

    @staticmethod
    def _symbol_kind(device: Device) -> str:
        """Classify a component from KiCad metadata and its reference."""
        family = _footprint_family(device.footprint_library).casefold().replace(" ", "")
        name = device.footprint_name.casefold()
        reference = device.reference.casefold()
        object_type = device.object_type.casefold().replace(" ", "")
        family = object_type or family
        if "connector" in family or "terminal" in family or reference.startswith("j"):
            return "connector"
        if "battery" in family or reference.startswith("bt"):
            return "battery"
        if "switch" in family or reference.startswith("sw"):
            return "switch"
        if "transistor" in family or "mosfet" in name or reference.startswith("q"):
            return "transistor"
        if "diode" in family or "led" in family or reference.startswith("d"):
            return "led" if "led" in family or "led" in name else "diode"
        if "capacitor" in family or "capacitor" in name or reference.startswith("c"):
            return "capacitor"
        if "resistor" in family or "resistor" in name or reference.startswith("r"):
            return "resistor"
        return "uc"

    @classmethod
    def _power_net_label(cls, name: str) -> str | None:
        """Return canonical power label for a case-insensitive net name.

        Args:
            name: Electrical net name.

        Returns:
            Canonical power label, or ``None`` for an ordinary net.
        """
        return cls._POWER_NETS.get(name.strip().casefold())

    @classmethod
    def _net_group_key(cls, name: str) -> str:
        """Return display grouping key, joining aliases of power nets.

        Args:
            name: Electrical net name.

        Returns:
            Stable display grouping key.
        """
        power_label = cls._power_net_label(name)
        if power_label is not None:
            return f"power:{power_label.casefold()}"
        return f"net:{name}"

    @classmethod
    def _net_display_name(cls, group_key: str) -> str:
        """Return user-facing name for a rendered net group.

        Args:
            group_key: Internal display grouping key.

        Returns:
            User-facing net or power label.
        """
        prefix, name = group_key.split(":", 1)
        if prefix == "power":
            return cls._POWER_NETS.get(name, name.upper())
        return name

    @classmethod
    def _draw_power_symbol(
        cls,
        painter: QPainter,
        stub: QPointF,
        outward: QPointF,
        label: str,
    ) -> None:
        """Draw compact global-power symbol at one net terminal.

        Args:
            painter: Schematic painter.
            stub: End of the terminal wire stub.
            outward: Unit vector pointing away from the component.
            label: Canonical power label.

        Returns:
            None.
        """
        normal = QPointF(-outward.y(), outward.x())
        tip = stub + outward * 18
        if label == "GND":
            for offset, width in ((0.0, 28.0), (7.0, 19.0), (14.0, 10.0)):
                center = tip + outward * offset
                painter.drawLine(
                    center - normal * (width / 2),
                    center + normal * (width / 2),
                )
        else:
            painter.drawLine(stub, tip)
            painter.drawText(
                QRectF(
                    tip.x() - 48,
                    tip.y() - 12,
                    96,
                    24,
                ),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )

    def _net_pen(self, selected: bool) -> QPen:
        """Build screen-width pen used for electrical net wires.

        Args:
            selected: Whether net is currently selected.

        Returns:
            Cosmetic pen that remains visible at overview zoom.
        """
        pen = QPen(
            self._color("selected_schematic_net")
            if selected
            else self._color("schematic_net"),
            self._SELECTED_NET_LINE_WIDTH if selected else self._NET_LINE_WIDTH,
        )
        pen.setCosmetic(True)
        return pen

    @staticmethod
    def _fast_net_route(start: QPointF, end: QPointF) -> list[QPointF]:
        """Return cheap two-segment route used during interactive movement.

        Args:
            start: Route origin.
            end: Route destination.

        Returns:
            Orthogonal polyline without obstacle search.
        """
        corner = QPointF(end.x(), start.y())
        return [start, corner, end]

    @staticmethod
    def _parallel_segments_conflict(
        first: tuple[QPointF, QPointF],
        second: tuple[QPointF, QPointF],
        clearance: float,
    ) -> bool:
        """Return whether parallel segments visually touch or overlap.

        Args:
            first: Candidate orthogonal segment.
            second: Previously displayed orthogonal segment.
            clearance: Required centerline spacing in logical units.

        Returns:
            True when projections overlap and centerlines are too close.
        """
        first_start, first_end = first
        second_start, second_end = second
        if first_start.x() == first_end.x() and second_start.x() == second_end.x():
            overlap = min(
                max(first_start.y(), first_end.y()),
                max(second_start.y(), second_end.y()),
            ) - max(
                min(first_start.y(), first_end.y()),
                min(second_start.y(), second_end.y()),
            )
            return overlap > 0.0 and abs(first_start.x() - second_start.x()) < clearance
        if first_start.y() == first_end.y() and second_start.y() == second_end.y():
            overlap = min(
                max(first_start.x(), first_end.x()),
                max(second_start.x(), second_end.x()),
            ) - max(
                min(first_start.x(), first_end.x()),
                min(second_start.x(), second_end.x()),
            )
            return overlap > 0.0 and abs(first_start.y() - second_start.y()) < clearance
        return False

    @classmethod
    def _display_lane_path(
        cls,
        path: list[QPointF],
        occupied: tuple[tuple[QPointF, QPointF], ...],
        clearance: float,
    ) -> list[QPointF]:
        """Offset collinear wire sections into non-touching display lanes.

        Args:
            path: Stable logical route.
            occupied: Segments already displayed for other nets.
            clearance: Required centerline spacing in logical units.

        Returns:
            Orthogonal display path; original terminal endpoints stay fixed.
        """
        if len(path) < 2 or not occupied:
            return path
        result = [path[0]]
        for start, end in zip(path, path[1:]):
            shifted_start = QPointF(start)
            shifted_end = QPointF(end)
            segment = (shifted_start, shifted_end)
            if any(
                cls._parallel_segments_conflict(segment, other, clearance)
                for other in occupied
            ):
                vertical = start.x() == end.x()
                for lane in range(1, 9):
                    found = False
                    for direction in (1.0, -1.0):
                        offset = lane * clearance * direction
                        if vertical:
                            candidate = (
                                QPointF(start.x() + offset, start.y()),
                                QPointF(end.x() + offset, end.y()),
                            )
                        else:
                            candidate = (
                                QPointF(start.x(), start.y() + offset),
                                QPointF(end.x(), end.y() + offset),
                            )
                        if not any(
                            cls._parallel_segments_conflict(candidate, other, clearance)
                            for other in occupied
                        ):
                            shifted_start, shifted_end = candidate
                            found = True
                            break
                    if found:
                        break
            current = result[-1]
            if current != shifted_start:
                if (
                    current.x() != shifted_start.x()
                    and current.y() != shifted_start.y()
                ):
                    result.append(QPointF(shifted_start.x(), current.y()))
                result.append(shifted_start)
            result.append(shifted_end)
        if result[-1] != path[-1]:
            current = result[-1]
            target = path[-1]
            if current.x() != target.x() and current.y() != target.y():
                result.append(QPointF(target.x(), current.y()))
            result.append(target)
        return OrthogonalRouter.simplify(result)

    @staticmethod
    def _route_label_point(path: list[QPointF]) -> QPointF:
        """Return midpoint of longest route segment for a readable NET label.

        Args:
            path: Displayed orthogonal route.

        Returns:
            Midpoint of longest segment, or first point for a degenerate path.
        """
        if len(path) < 2:
            return path[0]
        start, end = max(
            zip(path, path[1:]),
            key=lambda segment: QLineF(segment[0], segment[1]).length(),
        )
        return QPointF((start.x() + end.x()) / 2, (start.y() + end.y()) / 2)

    @staticmethod
    def _route_cache_key(
        start: QPointF,
        end: QPointF,
        obstacles: tuple[QRectF, ...],
        existing: tuple[tuple[QPointF, QPointF], ...],
    ) -> tuple:
        """Build route key from schematic geometry, excluding viewport state.

        Args:
            start: Route origin.
            end: Route destination.
            obstacles: Component bounds.
            existing: Previously routed segments.

        Returns:
            Immutable cache key.
        """
        return (
            (start.x(), start.y()),
            (end.x(), end.y()),
            tuple(
                (rect.left(), rect.top(), rect.width(), rect.height())
                for rect in obstacles
            ),
            tuple(
                ((left.x(), left.y()), (right.x(), right.y()))
                for left, right in existing
            ),
        )

    @staticmethod
    def _schemdraw_symbol(kind: str, pins: list[ComponentPin]):
        """Build one Schemdraw element and ordered anchor names."""
        from schemdraw import elements as elm  # pylint: disable=import-outside-toplevel

        if kind == "resistor":
            return elm.Resistor(), ("start", "end")
        if kind == "capacitor":
            return elm.Capacitor(), ("start", "end")
        if kind == "diode":
            return elm.Diode(), ("start", "end")
        if kind == "led":
            return elm.LED(), ("start", "end")
        if kind == "battery":
            return elm.Battery(), ("start", "end")
        if kind == "switch":
            return elm.Switch(), ("start", "end")
        if kind == "transistor":
            return elm.BjtNpn(), ("base", "collector", "emitter")
        if kind == "pad":
            return elm.Terminal(), ("end",)
        if kind == "connector":
            names = [pin.number for pin in pins]
            return elm.Header(rows=max(1, len(names)), pinsright=names), tuple(
                f"pin{index + 1}" for index in range(len(names))
            )
        side_pins = max(1, math.ceil(len(pins) / 4))
        sides = ("left", "top", "right", "bottom")
        schematic_pins = []
        for index, pin in enumerate(pins):
            side = sides[min(index // side_pins, len(sides) - 1)]
            schematic_pins.append(
                elm.IcPin(
                    name=None,
                    pin=pin.number,
                    side=side,
                    anchorname=f"tnasrevner_pin_{index}",
                )
            )
        return elm.Ic(
            pins=schematic_pins,
            edgepadW=0.65,
            edgepadH=0.65,
            pinspacing=1,
        ), tuple(f"tnasrevner_pin_{index}" for index in range(len(pins)))

    def _draw_schemdraw_symbol(
        self,
        painter: QPainter,
        kind: str,
        pins: list[ComponentPin],
        foreground: QColor | None = None,
    ) -> list[QPointF]:
        """Render one Schemdraw symbol and return its terminal endpoints.

        Args:
            painter: Painter receiving the rendered SVG symbol.
            kind: Schemdraw symbol family to render.
            pins: Ordered component pins used to build symbol anchors.
            foreground: Optional symbol stroke color; defaults to the normal
                schematic foreground.

        Returns:
            Rendered terminal endpoints in the symbol's local coordinate frame.
        """
        from schemdraw import Drawing  # pylint: disable=import-outside-toplevel

        symbol_color = (
            foreground
            if foreground is not None
            else QColor(self._SCHEMATIC_FOREGROUND)
        )
        color_name = symbol_color.name()
        cache_key = kind, tuple(pin.number for pin in pins), color_name
        cached = self._schemdraw_cache.get(cache_key)
        if cached is not None:
            renderer, target, endpoints = cached
            renderer.render(painter, target)
            return list(endpoints)

        element, anchor_names = self._schemdraw_symbol(kind, pins)
        drawing = Drawing(show=False, fontsize=10)
        drawing.add(element)
        svg = drawing.get_imagedata("svg").replace(
            b"black", color_name.encode("ascii")
        )
        renderer = QSvgRenderer(QByteArray(svg))
        viewbox = renderer.viewBoxF()
        element_bbox = element.get_bbox()
        center_anchor = element.absanchors.get("center")
        if center_anchor is None:
            center_anchor = (
                (element_bbox.xmin + element_bbox.xmax) / 2,
                (element_bbox.ymin + element_bbox.ymax) / 2,
            )

        def svg_coordinates(anchor) -> QPointF:
            anchor_x = anchor.x if hasattr(anchor, "x") else anchor[0]
            anchor_y = anchor.y if hasattr(anchor, "y") else anchor[1]
            return QPointF(
                anchor_x * self._SCHEMDRAW_UNIT_TO_SVG,
                -anchor_y * self._SCHEMDRAW_UNIT_TO_SVG,
            )

        center_svg = svg_coordinates(center_anchor)
        target = QRectF(
            -(center_svg.x() - viewbox.left()) / viewbox.width() * viewbox.width(),
            -(center_svg.y() - viewbox.top()) / viewbox.height() * viewbox.height(),
            viewbox.width(),
            viewbox.height(),
        )

        def map_anchor(anchor) -> QPointF:
            svg_point = svg_coordinates(anchor)
            return QPointF(
                target.left()
                + (svg_point.x() - viewbox.left()) / viewbox.width() * target.width(),
                target.top()
                + (svg_point.y() - viewbox.top()) / viewbox.height() * target.height(),
            )

        endpoints = tuple(map_anchor(element.absanchors[name]) for name in anchor_names)
        self._schemdraw_cache[cache_key] = renderer, target, endpoints
        renderer.render(painter, target)
        return list(endpoints)

    def _draw_device(
        self,
        painter: QPainter,
        device: Device,
        center: QPointF,
        net_points: dict[str, list[tuple[QPointF, QPointF]]],
    ) -> None:
        """Draw one symbol and collect its connected terminal positions."""
        pins = device.pins or [ComponentPin("?", "unmapped")]
        kind = self._symbol_kind(device)
        foreground = (
            self._color("schematic_glued")
            if device.schematic_glued
            else QColor(self._SCHEMATIC_FOREGROUND)
        )
        painter.setPen(QPen(foreground, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.save()
        painter.translate(center)
        painter.rotate(device.schematic_rotation)
        endpoints = self._draw_schemdraw_symbol(painter, kind, pins, foreground)
        transform = QTransform()
        transform.translate(center.x(), center.y())
        transform.rotate(device.schematic_rotation)
        endpoints = [transform.map(endpoint) for endpoint in endpoints]
        painter.restore()
        painter.setPen(foreground)
        compact_passive = kind in {"resistor", "capacitor"}
        if compact_passive:
            painter.drawText(
                QRectF(center.x() - 55, center.y() - 42, 110, 18),
                Qt.AlignmentFlag.AlignCenter,
                device.reference,
            )
        else:
            painter.drawText(
                int(center.x() - 70), int(center.y() - 48), device.reference
            )
        if device.value:
            painter.setPen(QColor("#aeb7c4"))
            if compact_passive:
                painter.drawText(
                    QRectF(center.x() - 55, center.y() + 24, 110, 18),
                    Qt.AlignmentFlag.AlignCenter,
                    device.value,
                )
            else:
                painter.drawText(
                    int(center.x() - 70), int(center.y() + 58), device.value
                )
        for index, pin in enumerate(pins):
            endpoint = endpoints[min(index, len(endpoints) - 1)]
            self._terminal_hits.append(
                (endpoint, ("pin", device.device_id, pin.number))
            )
            if (
                self._selected_net
                and not is_nc_net(self._selected_net)
                and pin.net_id == self._selected_net
            ):
                painter.setPen(QPen(self._color("connection_preview"), 4))
                painter.setBrush(self._color("connection_preview"))
                painter.drawEllipse(endpoint, 12, 12)
            if is_nc_net(pin.net_id):
                painter.setPen(QPen(self._color("new_connected_pad"), 3))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawLine(endpoint - QPointF(7, 7), endpoint + QPointF(7, 7))
                painter.drawLine(endpoint - QPointF(7, -7), endpoint + QPointF(7, -7))
            pin_color = self._color("schematic_net") if pin.net_id else QColor("#20242b")
            if self._selected_net and pin.net_id == self._selected_net:
                pin_color = self._color("connection_preview")
            painter.setPen(contrasting_text_color(pin_color))
            delta_x = endpoint.x() - center.x()
            delta_y = endpoint.y() - center.y()
            if kind == "uc":
                reference = pin.number
                function = pin.function.strip()
                if function == f"Pin {pin.number}":
                    function = ""
            else:
                reference, function = self._pin_annotation(device, pin)
            if abs(delta_x) >= abs(delta_y):
                horizontal_gap = 3.0 if compact_passive else 8.0
                if delta_x < 0:
                    text_x = endpoint.x() - 142 - horizontal_gap
                    alignment = Qt.AlignmentFlag.AlignRight
                else:
                    text_x = endpoint.x() + horizontal_gap
                    alignment = Qt.AlignmentFlag.AlignLeft
                text_y = endpoint.y() - (15 if compact_passive else 17)
                painter.drawText(QRectF(text_x, text_y, 142, 18), alignment, reference)
                if function:
                    painter.drawText(
                        QRectF(text_x, text_y + 15, 142, 18), alignment, function
                    )
            else:
                text_x = endpoint.x() - 75
                if compact_passive:
                    text_y = endpoint.y() - (21 if delta_y < 0 else -3)
                else:
                    text_y = endpoint.y() - (36 if delta_y < 0 else -8)
                painter.drawText(
                    QRectF(text_x, text_y, 150, 18),
                    Qt.AlignmentFlag.AlignCenter,
                    reference,
                )
                if function:
                    painter.drawText(
                        QRectF(text_x, text_y + 15, 150, 18),
                        Qt.AlignmentFlag.AlignCenter,
                        function,
                    )
            if pin.net_id:
                if abs(delta_x) >= abs(delta_y):
                    outward = QPointF(-1 if delta_x < 0 else 1, 0)
                else:
                    outward = QPointF(0, -1 if delta_y < 0 else 1)
                net_points.setdefault(self._net_group_key(pin.net_id), []).append(
                    (endpoint, outward)
                )

    def paintEvent(self, event) -> None:  # noqa: N802
        """Draw real component symbols, terminals, and net wires."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(event.rect(), QColor("#20242b"))
        painter.scale(self._zoom, self._zoom)
        painter.setClipRect(QRectF(0, 0, self._logical_width, self._logical_height))
        if self._project is None:
            painter.setPen(QColor("#aeb7c4"))
            painter.drawText(
                QRect(0, 0, self._logical_width, self._logical_height),
                Qt.AlignmentFlag.AlignCenter,
                "No project",
            )
            return
        devices = sorted(
            self._project.devices,
            key=lambda device: (
                0 if len(device.pins) > 4 else 1,
                _reference_sort_key(device.reference),
            ),
        )
        pads = sorted(self._project.pads, key=lambda pad: _natural_sort_key(pad.name))
        self._terminal_hits = []
        self._pad_centers = {}
        net_names = sorted(
            {
                self._net_group_key(pin.net_id)
                for device in devices
                for pin in device.pins
                if pin.net_id and not is_nc_net(pin.net_id)
            }
            | {
                self._net_group_key(pad.net)
                for pad in pads
                if pad.net and not is_nc_net(pad.net)
            }
        )
        if not devices and not pads:
            painter.setPen(QColor("#aeb7c4"))
            center = QPointF(self._WORLD_SIZE / 2, self._WORLD_SIZE / 2)
            painter.drawText(
                QRectF(center.x() - 240, center.y() - 20, 480, 40),
                Qt.AlignmentFlag.AlignCenter,
                "Place components or pads to build the schematic.",
            )
            return

        net_points: dict[str, list[tuple[QPointF, QPointF]]] = {}
        for index, device in enumerate(devices):
            center = self._center_for_device(device, index)
            self._device_centers[device.device_id] = center
            self._draw_device(
                painter,
                device,
                center,
                net_points,
            )
        device_net_points = {
            group_key: list(points) for group_key, points in net_points.items()
        }
        pad_only_centers = self._pad_only_net_centers(device_net_points)
        independent_pads = [pad for pad in pads if not pad.device_id]
        for index, pad in enumerate(independent_pads):
            pad_point, pad_outward = self._pad_render_geometry(
                index,
                len(independent_pads),
                pad,
                device_net_points,
                pad_only_centers,
            )
            self._pad_centers[pad.pad_id] = pad_point
            painter.save()
            painter.translate(pad_point)
            foreground = (
                self._color("schematic_glued")
                if pad.schematic_glued
                else QColor(self._SCHEMATIC_FOREGROUND)
            )
            painter.setPen(QPen(foreground, 2))
            if (
                self._selected_net
                and not is_nc_net(self._selected_net)
                and pad.net == self._selected_net
            ):
                painter.setPen(QPen(self._color("connection_preview"), 4))
                painter.setBrush(self._color("connection_preview"))
                painter.drawEllipse(QPointF(0, 0), 12, 12)
            if is_nc_net(pad.net):
                painter.setPen(QPen(self._color("new_connected_pad"), 3))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawLine(QPointF(-7, -7), QPointF(7, 7))
                painter.drawLine(QPointF(-7, 7), QPointF(7, -7))
            self._draw_schemdraw_symbol(
                painter,
                "pad",
                [ComponentPin(pad.number or pad.name, pad.name)],
                foreground,
            )
            painter.restore()
            pad_color = self._color("schematic_net") if pad.net else QColor("#20242b")
            if self._selected_net and pad.net == self._selected_net:
                pad_color = self._color("connection_preview")
            painter.setPen(
                foreground
                if pad.schematic_glued
                else contrasting_text_color(pad_color)
            )
            painter.drawText(int(pad_point.x() + 12), int(pad_point.y() - 16), pad.name)
            self._terminal_hits.append((pad_point, ("pad", pad.pad_id, None)))
            if pad.net and not is_nc_net(pad.net):
                net_points.setdefault(self._net_group_key(pad.net), []).append(
                    (pad_point, pad_outward)
                )
        painter.setPen(QPen(self._color("schematic_net"), 2))
        painter.setBrush(self._color("schematic_net"))
        obstacles = tuple(
            self._device_bounds(device, self._device_centers[device.device_id])
            for device in devices
        )
        routed_segments: list[tuple[QPointF, QPointF]] = []
        displayed_segments: list[tuple[QPointF, QPointF]] = []
        for group_key in net_names:
            name = self._net_display_name(group_key)
            selected = self._selected_net is not None and group_key == (
                self._net_group_key(self._selected_net)
            )
            painter.setPen(self._net_pen(selected))
            painter.setBrush(
                self._color("selected_schematic_net")
                if selected
                else self._color("schematic_net")
            )
            points = net_points.get(group_key, [])
            if not points:
                continue
            stubs = []
            for point, outward in points:
                stub = point + outward * 26
                painter.drawLine(point, stub)
                stubs.append(stub)
            if group_key.startswith("power:"):
                for stub, (_point, outward) in zip(stubs, points, strict=True):
                    self._draw_power_symbol(painter, stub, outward, name)
                continue
            routes = []
            pairs = self._minimum_spanning_pairs(stubs)
            degrees = [0] * len(stubs)
            other_net_segments = tuple(routed_segments)
            occupied_display = tuple(displayed_segments)
            current_net_segments: list[tuple[QPointF, QPointF]] = []
            current_display_segments: list[tuple[QPointF, QPointF]] = []
            for origin_index, target_index in pairs:
                degrees[origin_index] += 1
                degrees[target_index] += 1
                origin = stubs[origin_index]
                target = stubs[target_index]
                if self._drag_device_id is not None or self._drag_pad_id is not None:
                    path = self._fast_net_route(origin, target)
                else:
                    cache_key = self._route_cache_key(
                        origin, target, obstacles, other_net_segments
                    )
                    path = self._route_cache.get(cache_key)
                    if path is None:
                        path = OrthogonalRouter.route(
                            origin, target, obstacles, other_net_segments
                        )
                        if len(self._route_cache) >= 2048:
                            self._route_cache.clear()
                        self._route_cache[cache_key] = path
                display_path = self._display_lane_path(
                    path,
                    occupied_display,
                    (self._NET_LINE_WIDTH + 2.0) / self._zoom,
                )
                routes.append(display_path)
                for start, end in zip(display_path, display_path[1:]):
                    painter.drawLine(start, end)
                    current_display_segments.append((start, end))
                current_net_segments.extend(zip(path, path[1:]))
            routed_segments.extend(current_net_segments)
            displayed_segments.extend(current_display_segments)
            for index, degree in enumerate(degrees):
                if degree > 1:
                    painter.drawEllipse(stubs[index], 3, 3)
            if routes:
                label_point = self._route_label_point(routes[0])
                painter.drawText(
                    int(label_point.x() + 7), int(label_point.y() - 6), name
                )
            else:
                stub = stubs[0]
                outward = points[0][1]
                text_x = stub.x() + (7 if outward.x() >= 0 else -48)
                text_y = stub.y() + (16 if outward.y() > 0 else -6)
                painter.drawText(int(text_x), int(text_y), name)

        if (
            self._connection_preview_origin is not None
            and self._connection_preview_cursor is not None
        ):
            preview_pen = QPen(
                self._color("connection_preview"), 5.0, Qt.PenStyle.DashLine
            )
            preview_pen.setCosmetic(True)
            painter.setPen(preview_pen)
            painter.drawLine(
                self._connection_preview_origin,
                self._connection_preview_cursor,
            )

        orphan_y = 82 + len(devices) * 150
        for pad in pads:
            if pad.device_id or (pad.net and not is_nc_net(pad.net)):
                continue
            painter.setPen(QColor("#aeb7c4"))
            painter.drawText(24, orphan_y, f"Pad {pad.name} (unconnected)")
            orphan_y += 22


class SchematicView(QScrollArea):
    """Scrollable, zoomable schematic viewport."""

    zoom_changed = Signal(float)
    layout_changed = Signal(str, float, float)
    layout_started = Signal(str)
    layout_finished = Signal(str)
    layout_optimized = Signal()
    optimization_progress = Signal(int)
    optimization_finished = Signal()
    terminal_selected = Signal(object)
    terminal_hovered = Signal(object)
    terminal_net_edit_requested = Signal(object)
    terminal_menu_requested = Signal(object)
    device_context_requested = Signal(str)

    def __init__(
        self, parent: QWidget | None = None, config: AppConfig | None = None
    ) -> None:
        super().__init__(parent)
        self._zoom = 1.0
        self._canvas = SchematicCanvas(config=config)
        self._canvas.layout_changed.connect(self.layout_changed)
        self._canvas.layout_started.connect(self.layout_started)
        self._canvas.layout_finished.connect(self.layout_finished)
        self._canvas.layout_optimized.connect(self.layout_optimized)
        self._canvas.optimization_progress.connect(self.optimization_progress)
        self._canvas.optimization_finished.connect(self.optimization_finished)
        self._canvas.terminal_selected.connect(self.terminal_selected)
        self._canvas.terminal_hovered.connect(self.terminal_hovered)
        self._canvas.terminal_net_edit_requested.connect(
            self.terminal_net_edit_requested
        )
        self._canvas.terminal_menu_requested.connect(self.terminal_menu_requested)
        self._canvas.device_context_requested.connect(self.device_context_requested)
        self._canvas.pan_requested.connect(self._pan_by)
        self.setWidget(self._canvas)
        self.setWidgetResizable(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setMinimumSize(520, 320)
        self.setStyleSheet("QScrollArea { background: #20242b; }")
        self.viewport().setStyleSheet("background: #20242b;")
        self.setToolTip(
            "Wheel/pinch: zoom; Connect + Shift+click: select terminals; Esc: exit"
        )

    def set_project(self, project: ProjectDocument | None) -> None:
        """Set the project rendered by the schematic canvas."""
        self._canvas.set_project(project)

    def set_config(self, config: AppConfig) -> None:
        """Apply application colors to the schematic canvas.

        Args:
            config: Shared application display configuration.
        """
        self._canvas.set_config(config)

    def optimize_layout(self) -> None:
        """Run the explicit schematic layout optimization pass."""
        self._canvas.optimize_layout()

    def cancel_optimization(self) -> None:
        """Request cancellation of the active schematic optimization."""
        self._canvas.cancel_optimization()

    def fit_overview(self) -> None:
        """Fit and center the complete schematic sheet."""
        self._canvas.fit_overview()
        self._zoom = self._canvas.zoom()
        self.zoom_changed.emit(self._zoom)

    def actual_size(self) -> None:
        """Display schematic at logical 1:1 scale."""
        self._zoom = 1.0
        self._canvas.set_zoom(self._zoom)
        self.zoom_changed.emit(self._zoom)

    def set_selected_net(self, net: str | None) -> None:
        """Highlight one selected net in the schematic canvas."""
        self._canvas.set_selected_net(net)

    def view_state(self) -> tuple[float, int, int]:
        """Return schematic zoom and scroll positions."""
        return (
            self._zoom,
            self.horizontalScrollBar().value(),
            self.verticalScrollBar().value(),
        )

    def apply_view_state(self, state: tuple[float, int, int]) -> None:
        """Restore schematic zoom and scroll positions."""
        self._zoom = state[0]
        self._canvas.set_zoom(self._zoom)
        self.horizontalScrollBar().setValue(state[1])
        self.verticalScrollBar().setValue(state[2])
        self._canvas.update()

    def set_connection_mode(self, enabled: bool) -> None:
        """Set schematic cursor for connection editing."""
        self._canvas.set_connection_mode(enabled)

    def set_connection_preview_terminal(
        self, terminal: tuple[str, str, str | None]
    ) -> None:
        """Start the temporary schematic preview at one terminal."""
        self._canvas.set_connection_preview_terminal(terminal)

    def clear_connection_preview(self) -> None:
        """Clear the temporary schematic connection preview."""
        self._canvas.clear_connection_preview()

    def event(self, event) -> bool:  # noqa: N802
        """Zoom schematic with native trackpad pinch gestures."""
        if (
            event.type() == QEvent.Type.NativeGesture
            and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture
        ):
            self._zoom_by(max(0.01, 1.0 + event.value()))
            return True
        return super().event(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        """Zoom around the mouse position with the wheel."""
        if event.angleDelta().y() == 0:
            super().wheelEvent(event)
            return
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        cursor = self.viewport().mapFrom(self, event.position().toPoint())
        self._zoom_by(factor, cursor)
        event.accept()

    def _zoom_by(self, factor: float, cursor: QPoint | None = None) -> None:
        """Apply one zoom step around the cursor or viewport center."""
        old_zoom = self._zoom
        self._zoom = max(
            SchematicCanvas.MIN_ZOOM,
            min(SchematicCanvas.MAX_ZOOM, self._zoom * factor),
        )
        if self._zoom == old_zoom:
            return
        cursor = cursor or QPoint(
            self.viewport().width() // 2, self.viewport().height() // 2
        )
        source_x = (self.horizontalScrollBar().value() + cursor.x()) / old_zoom
        source_y = (self.verticalScrollBar().value() + cursor.y()) / old_zoom
        self._canvas.set_zoom(self._zoom)
        self.horizontalScrollBar().setValue(round(source_x * self._zoom - cursor.x()))
        self.verticalScrollBar().setValue(round(source_y * self._zoom - cursor.y()))
        self.zoom_changed.emit(self._zoom)

    def _pan_by(self, delta_x: int, delta_y: int) -> None:
        """Pan the schematic when dragging outside terminals/components."""
        self.horizontalScrollBar().setValue(
            self.horizontalScrollBar().value() - delta_x
        )
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta_y)
