"""Image import, editing, rendering, assets, and board image refresh."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,invalid-name,unused-import,duplicate-code
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


class ImageImportMixin:
    """Provide imageimport behavior to the main window."""

    def manage_picture(self) -> None:
        """Choose a side, then load it or open its image editor."""
        self._exit_connection_mode()
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        side = self._choose_image_side()
        if side is None:
            return
        if any(image.side == side for image in self.project.images):
            self.edit_picture(side)
        else:
            self.import_picture(side)

    def import_picture(self, side: str | None = None) -> None:
        """Import an image, then ask whether it belongs to top or bottom."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        source, _ = QFileDialog.getOpenFileName(
            self,
            "Import image",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)",
        )
        if not source:
            return
        source_path = Path(source)
        if QPixmap(str(source_path)).isNull():
            QMessageBox.warning(
                self, "Import failed", "Selected file is not a readable image."
            )
            return
        side = side or self._choose_image_side()
        if side is None:
            return
        source_image = QPixmap(str(source_path))
        edited = self._edit_imported_image(
            source_image, side=side, original_image=source_image
        )
        if edited is None:
            return
        if len(edited) == 4:
            edited = (*edited, (0.0, (0.0, 0.0, 1.0, 1.0)))
        if len(edited) == 6:
            edited = edited[:5]
        (
            edited_image,
            pixels_per_mm,
            calibration_line,
            calibration_length_mm,
            transformation,
        ) = edited
        original_path = f"assets/original/{side}{source_path.suffix.lower()}"
        try:
            self.store.write_asset(original_path, source_path.read_bytes())
        except OSError as error:
            QMessageBox.critical(self, "Import failed", str(error))
            return
        self.project.images = [
            image for image in self.project.images if image.side != side
        ]
        self.project.images.append(
            ImageAsset(
                side,
                original_path,
                source_path.name,
                pixels_per_mm,
                original_path,
                calibration_line,
                calibration_length_mm,
                (transformation,),
            )
        )
        self._image_cache.pop(side, None)
        self._rebuild_device_pads(side, edited_image)
        self._dirty = True
        self._refresh_views()
        self._update_title()

    def remove_picture(self, side: str | None = None) -> None:
        """Remove selected Top or Bottom image after explicit side choice."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        side = side or self._choose_image_side()
        if side is None:
            return
        asset = next(
            (image for image in self.project.images if image.side == side), None
        )
        if asset is None:
            QMessageBox.information(self, "No image", f"No {side} image imported.")
            return
        self.store.remove_asset(asset.path)
        if asset.original_path:
            self.store.remove_asset(asset.original_path)
        self.project.images = [
            image for image in self.project.images if image.side != side
        ]
        self._image_cache.pop(side, None)
        self._dirty = True
        self._refresh_views()
        self._update_title()

    def edit_picture(self, side: str | None = None) -> None:
        """Reopen the working image for crop and rotation adjustments."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        side = side or self._choose_image_side()
        if side is None:
            return
        asset = next(
            (image for image in self.project.images if image.side == side), None
        )
        if asset is None:
            QMessageBox.information(self, "No image", f"No {side} image imported.")
            return
        try:
            image = self._base_pixmap_for_asset(side)
        except ProjectFormatError as error:
            QMessageBox.warning(self, "Edit failed", str(error))
            return
        if image.isNull():
            QMessageBox.warning(self, "Edit failed", "Working image is unreadable.")
            return
        original_image = self._original_pixmap_for_asset(asset)
        edited = self._edit_imported_image(
            image,
            asset.pixels_per_mm,
            asset.calibration_line,
            asset.calibration_length_mm,
            side=side,
            original_image=original_image,
        )
        if edited is None:
            return
        if len(edited) == 4:
            edited = (*edited, (0.0, (0.0, 0.0, 1.0, 1.0)))
        reset_transformations = len(edited) >= 6 and bool(edited[5])
        if len(edited) == 6:
            edited = edited[:5]
        (
            edited_image,
            pixels_per_mm,
            calibration_line,
            calibration_length_mm,
            transformation,
        ) = edited
        transformations = (
            (transformation,)
            if reset_transformations
            else asset.transformations + (transformation,)
        )
        legacy_working_image = asset.original_path is not None and (
            asset.path != asset.original_path or not asset.transformations
        )
        if legacy_working_image:
            self.store.write_asset(asset.path, self._pixmap_bytes(edited_image))
        self.project.images = [
            (
                ImageAsset(
                    image.side,
                    image.path,
                    image.original_name,
                    pixels_per_mm,
                    image.original_path,
                    calibration_line,
                    calibration_length_mm,
                    transformations,
                )
                if image.side == side
                else image
            )
            for image in self.project.images
        ]
        self._image_cache.pop(side, None)
        self._rebuild_device_pads(side, edited_image)
        self._dirty = True
        self._refresh_views()
        self._update_title()

    def _edit_imported_image(
        self,
        image: QPixmap,
        pixels_per_mm: float | None = None,
        calibration_line: tuple[float, float, float, float] | None = None,
        calibration_length_mm: float | None = None,
        side: str | None = None,
        original_image: QPixmap | None = None,
    ) -> (  # pylint: disable=too-many-arguments,too-many-positional-arguments
        tuple[
            QPixmap,
            float,
            tuple[float, float, float, float] | None,
            float,
            tuple[float, tuple[float, float, float, float]],
            bool,
        ]
        | None
    ):
        """Open editor and return image plus scale, or `None` on cancel."""
        dialog = ImageEditDialog(
            image,
            self,
            footprint_selector=self._select_calibration_footprint,
            load_callback=(lambda: self.import_picture(side)) if side else None,
            original_image=original_image,
        )
        if pixels_per_mm is not None:
            dialog.prepare_existing_image(
                pixels_per_mm, calibration_line, calibration_length_mm
            )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return (
            dialog.result_pixmap(),
            dialog.pixels_per_mm(),
            dialog.calibration_line(),
            dialog.calibration_length_mm(),
            dialog.transformation(),
            dialog.started_from_original(),
        )

    @staticmethod
    def _pixmap_bytes(image: QPixmap) -> bytes:
        """Encode edited image as PNG bytes for archive storage."""
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.ReadWrite)
        if not image.save(buffer, "PNG"):
            raise ProjectFormatError("cannot encode imported image")
        return bytes(buffer.data())

    def _choose_image_side(self) -> str | None:
        """Ask which external board side the selected image represents."""
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Image side")
        dialog.setText("Where does this image belong?")
        top_button = dialog.addButton("Top", QMessageBox.ButtonRole.AcceptRole)
        bottom_button = dialog.addButton("Bottom", QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        dialog.exec()
        if dialog.clickedButton() is top_button:
            return "top"
        if dialog.clickedButton() is bottom_button:
            return "bottom"
        return None

    def _refresh_views(self) -> None:
        """Refresh all views from one composed pixmap per board side."""
        images = {side: self._pixmap_for_asset(side) for side in ("top", "bottom")}
        for side, view in self._views.items():
            view.set_trace_selection(self._selected_net, self._selected_pad_id)
            view.set_footprint_overlays(self._vector_footprints_for_side(side))
            view.set_pad_labels(self._vector_labels_for_side(side))
            view.set_pixmap(images[side])
        self._refresh_overlay(images)
        for side, view in self._side_views.items():
            view.set_trace_selection(self._selected_net, self._selected_pad_id)
            view.set_footprint_overlays(self._vector_footprints_for_side(side))
            view.set_pad_labels(self._vector_labels_for_side(side))
            view.set_pixmap(images[side])
        overlay_labels = self._vector_labels_for_side("top") + tuple(
            self._vector_labels_for_side("bottom")
        )
        self._overlay_view.set_trace_selection(
            self._selected_net, self._selected_pad_id
        )
        self._overlay_view.set_footprint_overlays(())
        self._overlay_view.set_pad_labels(overlay_labels)
        self._refresh_net_table()
        self._refresh_nets_table()
        self._refresh_bom_table()
        self._schematic_view.set_project(self.project)
        self._schematic_view.set_selected_net(self._selected_net)
        if self.project:
            self._tabs.setCurrentIndex(
                {
                    "top": 0,
                    "bottom": 1,
                    "side_by_side": 2,
                    "both": 3,
                    "nets": 4,
                    "net_summary": 5,
                    "bom": 6,
                    "schematic": 7,
                }[self.project.display.mode]
            )
            state = (
                self.project.display.zoom,
                self.project.display.pan_x,
                self.project.display.pan_y,
            )
            self._syncing_views = True
            try:
                for view in self._active_views():
                    view.apply_view_state(state)
            finally:
                self._syncing_views = False
            if self._tabs.currentIndex() < _CONNECTIONS_TAB:
                self._sync_board_views(self._active_views()[0])
        else:
            self._sync_board_views(self._views["top"])
        LOGGER.debug("View refresh completed")

    def _vector_labels_for_side(self, side: str) -> tuple[Pad, ...]:
        """Return visible pad labels for vector rendering in one view."""
        if self._pad_display_mode == "image" or not self.project:
            return ()
        return tuple(pad for pad in self.project.pads if pad.side == side)

    def _vector_footprints_for_side(self, side: str) -> tuple[tuple, ...]:
        """Return placed footprints for vector rendering in one view."""
        if self._pad_display_mode != "both" or not self.project:
            return ()
        image = next(
            (asset for asset in self.project.images if asset.side == side), None
        )
        pixmap = self._base_pixmap_for_asset(side)
        if image is None or pixmap.isNull():
            return ()
        pixels_per_mm = image.measured_pixels_per_mm(pixmap.width(), pixmap.height())
        if pixels_per_mm is None:
            pixels_per_mm = image.pixels_per_mm
        if pixels_per_mm is None:
            return ()
        overlays = []
        for device in self.project.devices:
            if device.side != side:
                continue
            footprint = self._footprint_for_device(device)
            if footprint is not None:
                overlays.append(
                    (
                        footprint,
                        device.x,
                        device.y,
                        device.rotation,
                        pixels_per_mm,
                        side,
                        pixmap.width(),
                    )
                )
        return tuple(overlays)

    def _refresh_overlay(self, images: dict[str, QPixmap]) -> None:
        """Compose top and bottom images in the same CMS top-view orientation."""
        top = images["top"]
        bottom = images["bottom"]
        base = top if not top.isNull() else bottom
        if base.isNull():
            self._overlay_view.set_pixmap(QPixmap())
            return
        canvas = QPixmap(base.size())
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        if not bottom.isNull():
            bottom = bottom.scaled(
                base.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.setOpacity(0.5)
            painter.drawPixmap(0, 0, bottom)
        if not top.isNull():
            top = top.scaled(
                base.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.setOpacity(0.5 if not bottom.isNull() else 1.0)
            painter.drawPixmap(0, 0, top)
        painter.end()
        self._overlay_view.set_pixmap(canvas)

    def _pixmap_for_asset(self, side: str) -> QPixmap:
        """Copy one cached working image and draw device/pad overlays."""
        pixmap = self._base_pixmap_for_asset(side)
        if pixmap.isNull():
            return pixmap
        if self._pad_display_mode == "pads":
            canvas = QPixmap(pixmap.size())
            canvas.fill(QColor(32, 33, 36))
            pixmap = canvas
        else:
            pixmap = QPixmap(pixmap)
        if self._pad_display_mode == "image":
            return pixmap
        return pixmap

    def _footprint_for_device(self, device: Device) -> Footprint | None:
        """Decode and cache an embedded footprint used by a placed device."""
        cached = self._device_footprint_cache.get(device.footprint_path)
        if cached is not None:
            return cached
        if self.store is None:
            return None
        try:
            footprint = parse_footprint(
                self.store.read_asset(device.footprint_path),
                device.footprint_library,
            )
        except (ProjectFormatError, KiCadFormatError) as error:
            LOGGER.warning(
                "Cannot render device footprint path=%s error=%s",
                device.footprint_path,
                error,
            )
            return None
        self._device_footprint_cache[device.footprint_path] = footprint
        return footprint

    def _rebuild_device_pads(self, side: str, pixmap: QPixmap) -> None:
        """Recalculate generated pads after an image is recalibrated."""
        if not self.project or pixmap.isNull():
            return
        image = next(
            (asset for asset in self.project.images if asset.side == side), None
        )
        if image is None:
            return
        pixels_per_mm = image.measured_pixels_per_mm(pixmap.width(), pixmap.height())
        if pixels_per_mm is None:
            return
        for device in [item for item in self.project.devices if item.side == side]:
            footprint = self._footprint_for_device(device)
            if footprint is None:
                continue
            try:
                placed_pads = place_footprint_pads(
                    footprint,
                    side,
                    device.x,
                    device.y,
                    device.rotation,
                    pixmap.width(),
                    pixmap.height(),
                    pixels_per_mm,
                )
            except KiCadFormatError as error:
                LOGGER.warning(
                    "Cannot rebuild pads device=%s error=%s",
                    device.reference,
                    error,
                )
                continue
            existing = {
                pad.number: pad
                for pad in self.project.pads
                if pad.device_id == device.device_id and pad.number is not None
            }
            rebuilt = []
            for placed in placed_pads:
                previous = existing.get(placed.number)
                if previous is None:
                    rebuilt.append(
                        Pad(
                            f"{device.reference}.{placed.number}",
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
                    )
                else:
                    rebuilt.append(
                        replace(
                            previous,
                            side=side,
                            x=placed.x,
                            y=placed.y,
                            width=placed.width,
                            height=placed.height,
                            shape=placed.shape,
                            rotation=placed.rotation,
                        )
                    )
            self.project.pads = [
                pad for pad in self.project.pads if pad.device_id != device.device_id
            ] + rebuilt
            LOGGER.info(
                "Rebuilt device pads reference=%s pixels_per_mm=%.6f pads=%s",
                device.reference,
                pixels_per_mm,
                len(rebuilt),
            )

    def _base_pixmap_for_asset(self, side: str) -> QPixmap:
        """Load the source image and replay its crop/rotation operations."""
        if not self.project or not self.store:
            return QPixmap()
        asset = next(
            (image for image in self.project.images if image.side == side), None
        )
        if not asset:
            return QPixmap()
        if side in self._image_cache:
            return self._image_cache[side]
        source_path = asset.path
        try:
            if self.store.is_archive:
                pixmap = QPixmap()
                pixmap.loadFromData(self.store.read_asset(source_path))
            else:
                pixmap = QPixmap(str(self.store.root / source_path))
        except ProjectFormatError:
            if not asset.original_path:
                QMessageBox.warning(
                    self, "Image unavailable", f"Missing image asset: {source_path}"
                )
                return QPixmap()
            source_path = asset.original_path
            if self.store.is_archive:
                pixmap = QPixmap()
                pixmap.loadFromData(self.store.read_asset(source_path))
            else:
                pixmap = QPixmap(str(self.store.root / source_path))
        if pixmap.isNull():
            QMessageBox.warning(
                self, "Image unavailable", f"Cannot decode image asset: {source_path}"
            )
            return pixmap
        if asset.transformations and source_path == asset.original_path:
            for rotation, crop in asset.transformations:
                if rotation:
                    pixmap = pixmap.transformed(
                        QTransform().rotate(rotation),
                        Qt.TransformationMode.SmoothTransformation,
                    )
                x, y, width, height = crop
                rect = QRect(
                    round(x * pixmap.width()),
                    round(y * pixmap.height()),
                    round(width * pixmap.width()),
                    round(height * pixmap.height()),
                ).intersected(pixmap.rect())
                if rect.width() >= 2 and rect.height() >= 2:
                    pixmap = pixmap.copy(rect)
        self._image_cache[side] = pixmap
        LOGGER.debug(
            "Cached working image side=%s size=%sx%s",
            side,
            pixmap.width(),
            pixmap.height(),
        )
        return pixmap

    def _original_pixmap_for_asset(self, asset: ImageAsset) -> QPixmap:
        """Load an image asset's untouched source without replaying transforms.

        Args:
            asset: Image metadata identifying the source stored in the project.

        Returns:
            The raw source pixmap, or a null pixmap when it cannot be loaded.
        """
        if not self.store:
            return QPixmap()
        source_path = asset.original_path or asset.path
        try:
            if self.store.is_archive:
                pixmap = QPixmap()
                pixmap.loadFromData(self.store.read_asset(source_path))
            else:
                pixmap = QPixmap(str(self.store.root / source_path))
        except ProjectFormatError:
            return QPixmap()
        return pixmap

    def _update_title(self, record_history: bool = True) -> None:
        """Refresh title and BOM, optionally recording a history snapshot.

        Args:
            record_history: Whether this UI refresh represents a completed edit.
        """
        if record_history:
            self._record_history()
        if hasattr(self, "_bom_table"):
            self._refresh_bom_table()
        name = self.project.project_name if self.project else "No project"
        self.setWindowTitle(f"Tnasrevner — {name}{' *' if self._dirty else ''}")
