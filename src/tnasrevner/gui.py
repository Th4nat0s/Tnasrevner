"""Basic desktop GUI for creating projects and displaying board pictures."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module

from pathlib import Path
import shutil
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QTabWidget,
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


class ImageView(QScrollArea):
    """Scrollable image view with mouse-wheel zoom."""

    def __init__(self, empty_text: str) -> None:
        super().__init__()
        self._empty_text = empty_text
        self._pixmap = QPixmap()
        self._scale = 1.0
        self._label = QLabel(empty_text)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setMinimumSize(240, 180)
        self.setWidget(self._label)
        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_image(self, path: Path | None) -> None:
        """Display image at its native scale, or show an empty state."""
        self._pixmap = QPixmap(str(path)) if path else QPixmap()
        self._scale = 1.0
        self._render()

    def wheelEvent(self, event) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Zoom image with Ctrl+wheel, preserving normal scroll behavior."""
        if (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and not self._pixmap.isNull()
        ):
            self._scale *= 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
            self._scale = max(0.1, min(self._scale, 20.0))
            self._render()
            event.accept()
            return
        super().wheelEvent(event)

    def _render(self) -> None:
        if self._pixmap.isNull():
            self._label.setPixmap(QPixmap())
            self._label.setText(self._empty_text)
            return
        self._label.setText("")
        self._label.setPixmap(
            self._pixmap.scaled(
                self._pixmap.size() * self._scale,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


class MainWindow(QMainWindow):  # pylint: disable=too-many-instance-attributes
    """Minimal project and board-picture workspace."""

    def __init__(self) -> None:
        super().__init__()
        self.project: ProjectDocument | None = None
        self.store: ProjectStore | None = None
        self._dirty = False
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
        side_layout = QVBoxLayout(side_by_side)
        self._side_views = {
            "top": ImageView("No top picture"),
            "bottom": ImageView("No bottom picture"),
        }
        side_layout.addWidget(self._side_views["top"])
        side_layout.addWidget(self._side_views["bottom"])
        self._tabs.addTab(side_by_side, "Top + bottom")
        self.setCentralWidget(self._tabs)
        self._create_actions()
        self._update_title()

    def _create_actions(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        for label, handler, shortcut in (
            ("New project", self.new_project, "Ctrl+N"),
            ("Open project", self.open_project, "Ctrl+O"),
            ("Save project", self.save_project, "Ctrl+S"),
            ("Import top picture", lambda: self.import_picture("top"), "Ctrl+T"),
            ("Import bottom picture", lambda: self.import_picture("bottom"), "Ctrl+B"),
        ):
            action = QAction(label, self)
            action.triggered.connect(handler)
            action.setShortcut(shortcut)
            file_menu.addAction(action)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    def new_project(self) -> None:
        """Create an empty project in a selected directory."""
        dialog = ProjectDetailsDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        root = QFileDialog.getExistingDirectory(self, "Project directory")
        if not root:
            return
        self.store = ProjectStore(Path(root))
        self.project = ProjectDocument(
            dialog.project_name.text(), dialog.board_name.text()
        )
        self._dirty = True
        self._refresh_views()
        self._update_title()

    def open_project(self) -> None:
        """Open a directory containing `project.json`."""
        root = QFileDialog.getExistingDirectory(self, "Open project")
        if not root:
            return
        try:
            store = ProjectStore(Path(root))
            project = store.load()
        except ProjectFormatError as error:
            QMessageBox.critical(self, "Open failed", str(error))
            return
        self.store, self.project, self._dirty = store, project, False
        self._refresh_views()
        self._update_title()

    def save_project(self) -> None:
        """Save project metadata and current display tab."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        self.project.display.mode = ("top", "bottom", "side_by_side")[
            self._tabs.currentIndex()
        ]
        self.store.save(self.project)
        self._dirty = False
        self._update_title()

    def import_picture(self, side: str) -> None:
        """Copy a selected picture into project assets and display it."""
        if not self.project or not self.store:
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        source, _ = QFileDialog.getOpenFileName(
            self,
            f"Import {side} picture",
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
        assets = self.store.root / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        destination = assets / f"{side}{source_path.suffix.lower()}"
        shutil.copy2(source_path, destination)
        self.project.images = [
            image for image in self.project.images if image.side != side
        ]
        self.project.images.append(
            ImageAsset(
                side,
                destination.relative_to(self.store.root).as_posix(),
                source_path.name,
            )
        )
        self._dirty = True
        self._refresh_views()
        self._update_title()

    def _refresh_views(self) -> None:
        """Reload both picture views from project-relative asset paths."""
        for side, view in {**self._views, **self._side_views}.items():
            asset = next(
                (
                    image
                    for image in (self.project.images if self.project else [])
                    if image.side == side
                ),
                None,
            )
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
