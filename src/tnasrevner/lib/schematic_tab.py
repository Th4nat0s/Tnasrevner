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
)

# pylint: disable=unused-import

from .app_support import _footprint_family, _reference_sort_key


class SchematicCanvas(QWidget):  # pylint: disable=too-many-instance-attributes
    """Paint a borderless, zoomable electrical schematic."""

    # Large enough to feel unbounded, small enough for Qt scroll/layout math.
    _WORLD_SIZE = 20_000.0
    MIN_ZOOM = 0.05
    MAX_ZOOM = 10.0
    _SCHEMDRAW_UNIT_TO_SVG = 36.0
    _NET_LINE_WIDTH = 5.0
    _SELECTED_NET_LINE_WIDTH = 7.0

    layout_changed = Signal(str, float, float)
    terminal_selected = Signal(object)
    terminal_hovered = Signal(object)
    terminal_net_edit_requested = Signal(object)
    terminal_menu_requested = Signal(object)
    device_context_requested = Signal(str)
    pan_requested = Signal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._project: ProjectDocument | None = None
        self._project_dirty = False
        self._zoom = 1.0
        self._logical_width = 1400
        self._logical_height = 1100
        self._auto_centers: dict[str, QPointF] = {}
        self._device_centers: dict[str, QPointF] = {}
        self._drag_device_id: str | None = None
        self._drag_offset = QPointF()
        self._terminal_hits: list[tuple[QPointF, tuple[str, str, str | None]]] = []
        self._selected_net: str | None = None
        self._connection_preview_origin: QPointF | None = None
        self._connection_preview_cursor: QPointF | None = None
        self._pan_position: QPoint | None = None
        self._schemdraw_cache: dict[
            tuple[str, tuple[str, ...]],
            tuple[QSvgRenderer, QRectF, tuple[QPointF, ...]],
        ] = {}
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(self._logical_width, self._logical_height)
        self.setAutoFillBackground(True)

    def set_project(self, project: ProjectDocument | None) -> None:
        """Replace the displayed project and repaint the schematic."""
        self._project = project
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
            point = self._independent_pad_center(index, len(independent_pads))
            item = QRectF(point.x() - 30, point.y() - 30, 60, 60)
            bounds = item if bounds is None else bounds.united(item)
        if bounds is None:
            center = QPointF(self._WORLD_SIZE / 2, self._WORLD_SIZE / 2)
            return QRectF(center.x() - 240, center.y() - 160, 480, 320)
        return bounds.adjusted(-80, -80, 80, 80)

    def _independent_pad_center(self, index: int, count: int) -> QPointF:
        """Return compact position for one independent schematic terminal."""
        bounds = self._content_bounds_without_pads()
        columns = max(1, min(6, count))
        return QPointF(
            bounds.center().x() + (index % columns - (columns - 1) / 2) * 150,
            bounds.bottom() + 120 + (index // columns) * 70,
        )

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
                if pin.net_id:
                    by_net.setdefault(pin.net_id, []).append(device.device_id)
        edges: set[tuple[str, str]] = set()
        for members in by_net.values():
            unique = sorted(set(members))
            for index, left in enumerate(unique):
                for right in unique[index + 1 :]:
                    edges.add((left, right))
        return edges

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
            return 110.0, 80.0
        return 150.0, 120.0

    @classmethod
    def _device_bounds(cls, device: Device, center: QPointF) -> QRectF:
        """Return a padded collision rectangle for one component."""
        width, height = cls._symbol_size(device)
        return QRectF(
            center.x() - width / 2 - 12,
            center.y() - height / 2 - 12,
            width + 24,
            height + 24,
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
            if terminal_hit is not None:
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
        self._drag_device_id = device_id
        self._drag_offset = point - self._device_centers[device_id]
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
        self._drag_device_id = None
        self._pan_position = None
        self.unsetCursor()
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
        self, painter: QPainter, kind: str, pins: list[ComponentPin]
    ) -> list[QPointF]:
        """Render one Schemdraw symbol and return its terminal endpoints."""
        from schemdraw import Drawing  # pylint: disable=import-outside-toplevel

        cache_key = kind, tuple(pin.number for pin in pins)
        cached = self._schemdraw_cache.get(cache_key)
        if cached is not None:
            renderer, target, endpoints = cached
            renderer.render(painter, target)
            return list(endpoints)

        element, anchor_names = self._schemdraw_symbol(kind, pins)
        drawing = Drawing(show=False, fontsize=10)
        drawing.add(element)
        svg = drawing.get_imagedata("svg").replace(b"black", b"#e7edf5")
        renderer = QSvgRenderer(QByteArray(svg))
        viewbox = renderer.viewBoxF()
        center_anchor = element.anchors.get("center")
        if center_anchor is None:
            bbox = element.get_bbox()
            center_anchor = (
                (bbox.xmin + bbox.xmax) / 2,
                (bbox.ymin + bbox.ymax) / 2,
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

        endpoints = tuple(map_anchor(element.anchors[name]) for name in anchor_names)
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
        painter.setPen(QPen(QColor("#e7edf5"), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.save()
        painter.translate(center)
        painter.rotate(device.schematic_rotation)
        endpoints = self._draw_schemdraw_symbol(painter, kind, pins)
        transform = QTransform()
        transform.translate(center.x(), center.y())
        transform.rotate(device.schematic_rotation)
        endpoints = [transform.map(endpoint) for endpoint in endpoints]
        painter.restore()
        painter.setPen(QColor("#f2f6fa"))
        painter.drawText(int(center.x() - 70), int(center.y() - 48), device.reference)
        if device.value:
            painter.setPen(QColor("#aeb7c4"))
            painter.drawText(int(center.x() - 70), int(center.y() + 58), device.value)
        for index, pin in enumerate(pins):
            endpoint = endpoints[min(index, len(endpoints) - 1)]
            self._terminal_hits.append(
                (endpoint, ("pin", device.device_id, pin.number))
            )
            painter.setPen(QColor("#c5d2dd"))
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
                if delta_x < 0:
                    text_x = endpoint.x() - 150
                    alignment = Qt.AlignmentFlag.AlignRight
                else:
                    text_x = endpoint.x() + 8
                    alignment = Qt.AlignmentFlag.AlignLeft
                text_y = endpoint.y() - 17
                painter.drawText(QRectF(text_x, text_y, 142, 18), alignment, reference)
                if function:
                    painter.drawText(
                        QRectF(text_x, text_y + 15, 142, 18), alignment, function
                    )
            else:
                text_x = endpoint.x() - 75
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
                net_points.setdefault(pin.net_id, []).append((endpoint, outward))

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
        pads = sorted(self._project.pads, key=lambda pad: pad.name)
        self._terminal_hits = []
        net_names = sorted(
            {pin.net_id for device in devices for pin in device.pins if pin.net_id}
            | {pad.net for pad in pads if pad.net}
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
        independent_pads = [pad for pad in pads if not pad.device_id]
        for index, pad in enumerate(independent_pads):
            pad_point = self._independent_pad_center(index, len(independent_pads))
            painter.save()
            painter.translate(pad_point)
            self._draw_schemdraw_symbol(
                painter,
                "pad",
                [ComponentPin(pad.number or pad.name, pad.name)],
            )
            painter.restore()
            painter.setPen(QColor("#c5d2dd"))
            painter.drawText(int(pad_point.x() + 12), int(pad_point.y() - 16), pad.name)
            self._terminal_hits.append((pad_point, ("pad", pad.pad_id, None)))
            if pad.net:
                net_points.setdefault(pad.net, []).append((pad_point, QPointF(1, 0)))
        painter.setPen(QPen(QColor("#e4b363"), 2))
        painter.setBrush(QColor("#e4b363"))
        for name in net_names:
            selected = name == self._selected_net
            painter.setPen(
                QPen(
                    QColor("#66c2ff") if selected else QColor("#e4b363"),
                    (
                        self._SELECTED_NET_LINE_WIDTH
                        if selected
                        else self._NET_LINE_WIDTH
                    ),
                )
            )
            painter.setBrush(QColor("#66c2ff") if selected else QColor("#e4b363"))
            points = net_points.get(name, [])
            if not points:
                continue
            stubs = []
            for point, outward in points:
                stub = point + outward * 26
                painter.drawLine(point, stub)
                stubs.append(stub)
            if len(stubs) == 2:
                middle_x = (stubs[0].x() + stubs[1].x()) / 2
                painter.drawLine(stubs[0], QPointF(middle_x, stubs[0].y()))
                painter.drawLine(
                    QPointF(middle_x, stubs[0].y()),
                    QPointF(middle_x, stubs[1].y()),
                )
                painter.drawLine(QPointF(middle_x, stubs[1].y()), stubs[1])
                painter.drawEllipse(QPointF(middle_x, stubs[0].y()), 3, 3)
                painter.drawText(int(middle_x + 7), int(stubs[0].y() - 6), name)
            else:
                for stub, (_point, outward) in zip(stubs, points, strict=True):
                    text_x = stub.x() + (7 if outward.x() >= 0 else -48)
                    text_y = stub.y() + (16 if outward.y() > 0 else -6)
                    painter.drawText(int(text_x), int(text_y), name)

        if (
            self._connection_preview_origin is not None
            and self._connection_preview_cursor is not None
        ):
            preview_pen = QPen(QColor("#66c2ff"), 5.0, Qt.PenStyle.DashLine)
            preview_pen.setCosmetic(True)
            painter.setPen(preview_pen)
            painter.drawLine(
                self._connection_preview_origin,
                self._connection_preview_cursor,
            )

        orphan_y = 82 + len(devices) * 150
        for pad in pads:
            if pad.device_id or pad.net:
                continue
            painter.setPen(QColor("#aeb7c4"))
            painter.drawText(24, orphan_y, f"Pad {pad.name} (unconnected)")
            orphan_y += 22


class SchematicView(QScrollArea):
    """Scrollable, zoomable schematic viewport."""

    zoom_changed = Signal(float)
    layout_changed = Signal(str, float, float)
    terminal_selected = Signal(object)
    terminal_hovered = Signal(object)
    terminal_net_edit_requested = Signal(object)
    terminal_menu_requested = Signal(object)
    device_context_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._zoom = 1.0
        self._canvas = SchematicCanvas()
        self._canvas.layout_changed.connect(self.layout_changed)
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
