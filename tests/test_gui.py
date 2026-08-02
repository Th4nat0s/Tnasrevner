"""Functional tests for minimal project lifecycle GUI actions."""

# Qt uses compiled extension modules; test fixtures intentionally inspect GUI
# state to verify lifecycle behavior.
# pylint: disable=wrong-import-position,no-name-in-module,redefined-outer-name
# pylint: disable=unused-argument,protected-access

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from tnasrevner.gui import MainWindow
from tnasrevner.project import ProjectDocument, ProjectStore


@pytest.fixture(scope="module")
def app() -> QApplication:
    """Provide one headless Qt application for GUI tests."""
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app: QApplication) -> MainWindow:
    """Create isolated main window."""
    result = MainWindow(show_startup=False)
    yield result
    result._dirty = False
    result.close()


def test_create_save_close_reopen_project(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise project creation, archive save, close, and reopen."""
    archive = tmp_path / "board.revp"

    class FakeDialog:  # pylint: disable=too-few-public-methods
        """Replacement project dialog for non-interactive testing."""

        project_name = SimpleNamespace(text=lambda: "Project")
        board_name = SimpleNamespace(text=lambda: "Board")

        def __init__(self, _parent) -> None:
            pass

        def exec(self) -> QDialog.DialogCode:
            """Pretend user accepted project details."""
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr("tnasrevner.gui.ProjectDetailsDialog", FakeDialog)
    monkeypatch.setattr(
        "tnasrevner.gui.QFileDialog.getSaveFileName",
        lambda *_args: (str(archive), ""),
    )
    monkeypatch.setattr(
        "tnasrevner.gui.QFileDialog.getOpenFileName",
        lambda *_args: (str(archive), ""),
    )

    window.new_project()
    assert window.project is not None
    assert window.save_project()
    assert archive.is_file()
    window.close_project()
    assert window.project is None

    window.open_project()
    assert window.project is not None
    assert window.project.project_name == "Project"
    assert window.project.board_name == "Board"


def test_close_project_cancel_preserves_dirty_project(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel in save guard must keep project and unsaved state."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    window._dirty = True
    monkeypatch.setattr(
        "tnasrevner.gui.QMessageBox.warning",
        lambda *_args: QMessageBox.StandardButton.Cancel,
    )

    window.close_project()

    assert window.project is not None
    assert window._dirty


def test_open_invalid_archive_reports_error(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid archive must leave current project unchanged and report failure."""
    invalid = tmp_path / "invalid.revp"
    invalid.write_bytes(b"not a zip")
    errors: list[str] = []
    monkeypatch.setattr(
        "tnasrevner.gui.QFileDialog.getOpenFileName",
        lambda *_args: (str(invalid), ""),
    )
    monkeypatch.setattr(
        "tnasrevner.gui.QMessageBox.critical",
        lambda _parent, _title, message: errors.append(message),
    )

    window.open_project()

    assert window.project is None
    assert errors and "cannot read project archive" in errors[0]


def test_import_image_stores_selected_side(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One import action stores image after side selection."""
    source = tmp_path / "board-photo.png"
    image = QImage(10, 10, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    assert image.save(str(source))
    archive = tmp_path / "board.revp"
    window.store = ProjectStore(archive)
    window.project = ProjectDocument("Project", "Board")
    monkeypatch.setattr(
        "tnasrevner.gui.QFileDialog.getOpenFileName",
        lambda *_args: (str(source), ""),
    )
    monkeypatch.setattr(window, "_choose_image_side", lambda: "top")
    monkeypatch.setattr(window, "_edit_imported_image", lambda image: image)

    window.import_picture()

    assert window.project.images[0].side == "top"
    assert window.store.read_asset("assets/top.png").startswith(b"\x89PNG")
