"""Basic desktop GUI for creating projects and displaying board pictures."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,invalid-name

from pathlib import Path
import sys
from time import monotonic

from PySide6.QtCore import QBuffer, QEvent, QIODevice, QPoint, QRect, QTimer, Qt
from PySide6.QtGui import QAction, QCloseEvent, QCursor, QPixmap, QTransform
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QTabWidget,
    QPushButton,
    QRubberBand,
    QVBoxLayout,
    QWidget,
)

from .project import ImageAsset, ProjectDocument, ProjectFormatError, ProjectStore


class ProjectDetailsDialog(QDialog):  # pylint: disable=too-few-public-methods
    """Collect the names needed to create a project."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New project")
        self.project_name = QLineEdit()
        self.board_name = QLineEdit()
        form = QFormLayout(self)
        form.addRow("Project name", self.project_name)
        form.addRow("Board name", self.board_name)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self) -> None:
        """Reject empty names before closing the dialog."""
        if not self.project_name.text().strip() or not self.board_name.text().strip():
            QMessageBox.warning(self, "Missing name", "Enter project and board names.")
            return
        super().accept()


class StartupDialog(QMessageBox):  # pylint: disable=too-few-public-methods
    """Ask whether to load an existing project or create a new one."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Tnasrevner")
        self.setText("What do you want to do?")
        self.load_button = self.addButton(
            "Load project", QMessageBox.ButtonRole.AcceptRole
        )
        self.new_button = self.addButton(
            "New project", QMessageBox.ButtonRole.AcceptRole
        )
        self.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)

    def choice(self) -> str | None:
        """Return `load`, `new`, or `None` after dialog closes."""
        self.exec()
        if self.clickedButton() is self.load_button:
            return "load"
        if self.clickedButton() is self.new_button:
            return "new"
        return None


class ImageEditDialog(QDialog):  # pylint: disable=too-many-instance-attributes
    """Rotate and crop an image before it enters a project archive."""

    def __init__(self, image: QPixmap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit imported image")
        self.resize(1000, 700)
        self._source = image
        self._display_scale = 1.0
        self._selection_start: QPoint | None = None
        self._selection = None
        self._canvas = QLabel()
        self._canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._canvas.installEventFilter(self)
        self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self._canvas)
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._canvas)
        self._scroll.setWidgetResizable(False)
        rotate_left = QPushButton("Rotate left")
        rotate_left.clicked.connect(lambda: self._rotate(-90))
        rotate_right = QPushButton("Rotate right")
        rotate_right.clicked.connect(lambda: self._rotate(90))
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        controls = QHBoxLayout()
        controls.addWidget(rotate_left)
        controls.addWidget(rotate_right)
        controls.addStretch()
        controls.addWidget(self._buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Drag a rectangle over the board area to import."))
        layout.addWidget(self._scroll)
        layout.addLayout(controls)
        self._render()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """Track rectangle selection on the image canvas."""
        if watched is not self._canvas:
            return super().eventFilter(watched, event)
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._selection_start = event.position().toPoint()
            self._selection = None
            self._rubber_band.hide()
            self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return True
        if event.type() == QEvent.Type.MouseMove and self._selection_start is not None:
            self._set_selection(self._selection_start, event.position().toPoint())
            return True
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and self._selection_start is not None
        ):
            self._set_selection(self._selection_start, event.position().toPoint())
            self._selection_start = None
            return True
        return super().eventFilter(watched, event)

    def accept(self) -> None:
        """Crop selected area and close editor."""
        if (
            self._selection is None
            or self._selection.width() < 2
            or self._selection.height() < 2
        ):
            QMessageBox.warning(self, "Select area", "Select a rectangle first.")
            return
        selection = self._selection
        source_rect = self._source_rect(selection)
        self._source = self._source.copy(source_rect)
        super().accept()

    def result_pixmap(self) -> QPixmap:
        """Return edited image after accepted dialog."""
        return self._source

    def _rotate(self, angle: int) -> None:
        """Rotate source image and clear old selection."""
        self._source = self._source.transformed(
            QTransform().rotate(angle), Qt.TransformationMode.SmoothTransformation
        )
        self._selection = None
        self._rubber_band.hide()
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._render()

    def _render(self) -> None:
        """Fit source image to editor viewport."""
        viewport = self._scroll.viewport().size()
        width_ratio = viewport.width() / self._source.width()
        height_ratio = viewport.height() / self._source.height()
        self._display_scale = min(1.0, width_ratio, height_ratio)
        displayed = self._source.scaled(
            self._source.size() * self._display_scale,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._canvas.setPixmap(displayed)
        self._canvas.resize(displayed.size())

    def _set_selection(self, start: QPoint, end: QPoint) -> None:
        """Update selection rectangle constrained to displayed image."""
        bounds = self._canvas.rect()
        start = self._clamp_point(start, bounds)
        end = self._clamp_point(end, bounds)
        self._selection = QRect(start, end).normalized()
        self._rubber_band.setGeometry(self._selection)
        self._rubber_band.show()
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self._selection.width() >= 2 and self._selection.height() >= 2
        )

    @staticmethod
    def _clamp_point(point: QPoint, bounds: QRect) -> QPoint:
        """Constrain point to image canvas bounds."""
        return QPoint(
            max(bounds.left(), min(point.x(), bounds.right())),
            max(bounds.top(), min(point.y(), bounds.bottom())),
        )

    def _source_rect(self, selection: QRect) -> QRect:
        """Convert displayed selection to source-pixel coordinates."""
        return QRect(
            int(selection.x() / self._display_scale),
            int(selection.y() / self._display_scale),
            int(selection.width() / self._display_scale),
            int(selection.height() / self._display_scale),
        )


class ImageView(QScrollArea):
    """Scrollable image view with mouse-wheel zoom."""

    def __init__(self, empty_text: str) -> None:
        super().__init__()
        self._empty_text = empty_text
        self._pixmap = QPixmap()
        self._scale = 1.0
        self._drag_position: QPoint | None = None
        self._label = QLabel(empty_text)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setMinimumSize(240, 180)
        self._label.installEventFilter(self)
        self.viewport().installEventFilter(self)
        self.setWidget(self._label)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_image(self, path: Path | None) -> None:
        """Display image at its native scale, or show an empty state."""
        self._pixmap = QPixmap(str(path)) if path else QPixmap()
        self._scale = 1.0
        self._render()

    def set_image_data(self, content: bytes) -> None:
        """Display image bytes loaded from a `.revp` archive."""
        self._pixmap = QPixmap()
        self._pixmap.loadFromData(content)
        self._scale = 1.0
        self._render()

    def wheelEvent(self, event) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Zoom image with Ctrl+wheel, preserving normal scroll behavior."""
        if (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and not self._pixmap.isNull()
        ):
            self._zoom_by(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
            event.accept()
            return
        super().wheelEvent(event)

    def event(self, event) -> bool:  # noqa: N802
        """Handle macOS trackpad pinch-to-zoom gestures."""
        if (
            event.type() == QEvent.Type.NativeGesture
            and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture
            and not self._pixmap.isNull()
        ):
            self._zoom_by(max(0.01, 1.0 + event.value()))
            return True
        return super().event(event)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """Pan image by dragging it with the primary mouse button."""
        if watched not in (self._label, self.viewport()):
            return super().eventFilter(watched, event)
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._drag_position = event.position().toPoint()
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return True
        if event.type() == QEvent.Type.MouseMove and self._drag_position is not None:
            current = event.position().toPoint()
            delta = current - self._drag_position
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )
            self._drag_position = current
            return True
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and self._drag_position is not None
        ):
            self._drag_position = None
            self.unsetCursor()
            return True
        return super().eventFilter(watched, event)

    def _zoom_by(self, factor: float) -> None:
        """Apply zoom factor and keep it in a usable range."""
        self._scale = max(0.1, min(self._scale * factor, 20.0))
        self._render()

    def _render(self) -> None:
        if self._pixmap.isNull():
            self._label.setPixmap(QPixmap())
            self._label.setText(self._empty_text)
            return
        self._label.setText("")
        fit_scale = self._fit_scale()
        self._label.setPixmap(
            self._pixmap.scaled(
                self._pixmap.size() * (fit_scale * self._scale),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._label.resize(self._label.pixmap().size())

    def resizeEvent(self, event) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Keep the image fitted after resizing its view."""
        self._render()
        super().resizeEvent(event)

    def fit_image(self) -> None:
        """Fit image inside current view."""
        self._scale = 1.0
        self._render()

    def actual_size(self) -> None:
        """Show image at 1:1 source-pixel scale."""
        fit_scale = self._fit_scale()
        self._scale = 1.0 / fit_scale if fit_scale else 1.0
        self._render()

    def center_image(self) -> None:
        """Center current image in available scrollable area."""
        horizontal = self.horizontalScrollBar()
        vertical = self.verticalScrollBar()
        horizontal.setValue((horizontal.maximum() + horizontal.minimum()) // 2)
        vertical.setValue((vertical.maximum() + vertical.minimum()) // 2)

    def _fit_scale(self) -> float:
        """Calculate scale needed to fit image in viewport."""
        if self._pixmap.isNull():
            return 1.0
        viewport = self.viewport().size()
        width_ratio = viewport.width() / self._pixmap.width()
        height_ratio = viewport.height() / self._pixmap.height()
        return min(1.0, width_ratio, height_ratio)


class MainWindow(QMainWindow):  # pylint: disable=too-many-instance-attributes
    """Minimal project and board-picture workspace."""

    def __init__(self, show_startup: bool = True) -> None:
        super().__init__()
        self.project: ProjectDocument | None = None
        self.store: ProjectStore | None = None
        self._dirty = False
        self._last_view_key: int | None = None
        self._last_view_time = 0.0
        self.setWindowTitle("Tnasrevner")
        self.resize(1100, 700)
        self._views = {
            "top": ImageView("No top picture"),
            "bottom": ImageView("No bottom picture"),
        }
        self._tabs = QTabWidget()
        self._tabs.addTab(self._views["top"], "Top")
        self._tabs.addTab(self._views["bottom"], "Bottom")
        side_by_side = QWidget()
        side_layout = QHBoxLayout(side_by_side)
        self._side_views = {
            "top": ImageView("No top picture"),
            "bottom": ImageView("No bottom picture"),
        }
        side_layout.addWidget(self._side_views["top"])
        side_layout.addWidget(self._side_views["bottom"])
        self._tabs.addTab(side_by_side, "Top + bottom")
        self.setCentralWidget(self._tabs)
        self._create_actions()
        self._create_tool_palette()
        self._update_title()
        if show_startup:
            QTimer.singleShot(0, self._startup_choice)

    def _create_tool_palette(self) -> None:
        """Create the right-side view control palette."""
        dock = QDockWidget("Tools", self)
        panel = QWidget(dock)
        layout = QVBoxLayout(panel)
        actual_button = QPushButton("1:1", panel)
        actual_button.setToolTip("Actual image size")
        actual_button.clicked.connect(self._actual_size)
        layout.addWidget(actual_button)
        fit_button = QPushButton("FIT", panel)
        fit_button.setToolTip("Fit image in view")
        fit_button.clicked.connect(self._fit_images)
        layout.addWidget(fit_button)
        center_button = QPushButton("◎", panel)
        center_button.setToolTip("Center image")
        center_button.clicked.connect(self._center_images)
        layout.addWidget(center_button)
        layout.addSpacing(12)
        import_button = QPushButton("Import image", panel)
        import_button.setToolTip("Import image, then choose Top or Bottom")
        import_button.clicked.connect(self.import_picture)
        layout.addWidget(import_button)
        layout.addStretch()
        panel.setLayout(layout)
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def _startup_choice(self) -> None:
        """Show startup choice and open the selected workflow."""
        choice = StartupDialog(self).choice()
        if choice == "load":
            self.open_project()
        elif choice == "new":
            self.new_project()

    def _active_views(self) -> list[ImageView]:
        """Return image views belonging to current tab."""
        if self._tabs.currentIndex() == 0:
            return [self._views["top"]]
        if self._tabs.currentIndex() == 1:
            return [self._views["bottom"]]
        return list(self._side_views.values())

    def _actual_size(self) -> None:
        """Set active image view(s) to 1:1 scale."""
        for view in self._active_views():
            view.actual_size()

    def _fit_images(self) -> None:
        """Fit active image view(s) to their available space."""
        for view in self._active_views():
            view.fit_image()

    def _center_images(self) -> None:
        """Center active image view(s)."""
        for view in self._active_views():
            view.center_image()

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
        self._import_action = QAction("Import image", self)
        self._import_action.setShortcut("I")
        self._import_action.setToolTip("Import image, then choose Top or Bottom")
        self._import_action.triggered.connect(self.import_picture)
        file_menu.addAction(self._import_action)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """Switch board view with `T`, `B`, or a double press."""
        key = event.key()
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

    def new_project(self) -> None:
        """Create an empty `.revp` project file."""
        if not self._confirm_pending_changes():
            return
        dialog = ProjectDetailsDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Create project", "", "Tnasrevner project (*.revp)"
        )
        if not path:
            return
        project_path = Path(path)
        if project_path.suffix.lower() != ".revp":
            project_path = project_path.with_suffix(".revp")
        self.store = ProjectStore(project_path)
        self.project = ProjectDocument(
            dialog.project_name.text(), dialog.board_name.text()
        )
        self._dirty = True
        self._refresh_views()
        self._update_title()

    def open_project(self) -> None:
        """Open a `.revp` project file."""
        if not self._confirm_pending_changes():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", "", "Tnasrevner project (*.revp)"
        )
        if not path:
            return
        try:
            store = ProjectStore(Path(path))
            project = store.load()
        except ProjectFormatError as error:
            QMessageBox.critical(self, "Open failed", str(error))
            return
        self.store, self.project, self._dirty = store, project, False
        self._refresh_views()
        self._update_title()

    def save_project(self) -> bool:
        """Save project metadata and current display tab."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return False
        self.project.display.mode = ("top", "bottom", "side_by_side")[
            self._tabs.currentIndex()
        ]
        try:
            self.store.save(self.project)
        except (OSError, ProjectFormatError) as error:
            QMessageBox.critical(self, "Save failed", str(error))
            return False
        self._dirty = False
        self._update_title()
        return True

    def close_project(self) -> None:
        """Close current project after resolving pending changes."""
        if not self._confirm_pending_changes():
            return
        self.project = None
        self.store = None
        self._dirty = False
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
        if self._confirm_pending_changes():
            event.accept()
        else:
            event.ignore()

    def import_picture(self) -> None:
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
        side = self._choose_image_side()
        if side is None:
            return
        edited_image = self._edit_imported_image(QPixmap(str(source_path)))
        if edited_image is None:
            return
        relative_path = f"assets/{side}.png"
        self.store.write_asset(relative_path, self._pixmap_bytes(edited_image))
        self.project.images = [
            image for image in self.project.images if image.side != side
        ]
        self.project.images.append(
            ImageAsset(
                side,
                relative_path,
                source_path.name,
            )
        )
        self._dirty = True
        self._refresh_views()
        self._update_title()

    def _edit_imported_image(self, image: QPixmap) -> QPixmap | None:
        """Open image editor and return edited image, or `None` on cancel."""
        dialog = ImageEditDialog(image, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.result_pixmap()

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
        """Reload both picture views from project-relative asset paths."""
        for side, view in self._views.items():
            asset = next(
                (
                    image
                    for image in (self.project.images if self.project else [])
                    if image.side == side
                ),
                None,
            )
            if asset and self.store and self.store.is_archive:
                view.set_image_data(self.store.read_asset(asset.path))
            else:
                path = self.store.root / asset.path if asset and self.store else None
                view.set_image(path if path and path.is_file() else None)
        for side, view in self._side_views.items():
            asset = next(
                (
                    image
                    for image in (self.project.images if self.project else [])
                    if image.side == side
                ),
                None,
            )
            if asset and self.store and self.store.is_archive:
                view.set_image_data(self.store.read_asset(asset.path))
            else:
                path = self.store.root / asset.path if asset and self.store else None
                view.set_image(path if path and path.is_file() else None)
        if self.project:
            self._tabs.setCurrentIndex(
                {"top": 0, "bottom": 1, "side_by_side": 2}[self.project.display.mode]
            )

    def _update_title(self) -> None:
        name = self.project.project_name if self.project else "No project"
        self.setWindowTitle(f"Tnasrevner — {name}{' *' if self._dirty else ''}")


def main() -> int:
    """Run Tnasrevner GUI."""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
