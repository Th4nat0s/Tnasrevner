"""Configuration tab for application display preferences."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QFormLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .app_config import AppConfig, DEFAULT_COLORS

COLOR_LABELS = {
    "connected_pad": "Connected Pad",
    "unconnected_pad": "Unconnected Pad",
    "new_connected_pad": "New Connected Pad",
    "connected_pad_1": "Connected Pad 1",
    "unconnected_pad_1": "Unconnected Pad 1",
    "connection_preview": "Connection Preview",
    "connection_line": "Connection Line",
    "selected_terminal": "Selected Terminal",
    "schematic_net": "Schematic Net",
    "selected_schematic_net": "Selected Schematic Net",
}


class ConfigPage(QWidget):
    """Expose supported application colors and persist user changes."""

    def __init__(self, config: AppConfig, changed: Callable[[], None]) -> None:
        """Create the color settings page.

        Args:
            config: Shared application configuration.
            changed: Callback invoked after a color selection or save.
        """
        super().__init__()
        self._config = config
        self._changed = changed
        self._buttons: dict[str, QPushButton] = {}
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Application display colors (saved in config.yaml)"))
        form = QFormLayout()
        for key in DEFAULT_COLORS:
            button = QPushButton()
            button.setAccessibleName(COLOR_LABELS[key])
            button.clicked.connect(
                lambda _checked=False, color_key=key: self._choose_color(color_key)
            )
            self._buttons[key] = button
            form.addRow(COLOR_LABELS[key], button)
            self._update_button(key)
        layout.addLayout(form)
        save = QPushButton("Save configuration")
        save.clicked.connect(self._save)
        layout.addWidget(save)
        layout.addStretch()

    def _update_button(self, key: str) -> None:
        """Update one picker preview to its effective color.

        Args:
            key: Semantic color key.
        """
        color = self._config.colors[key]
        self._buttons[key].setText(color)
        text_color = "black" if color.lower() in {"#ffffff", "#ffff00"} else "white"
        self._buttons[key].setStyleSheet(
            f"background-color: {color}; color: {text_color};"
        )

    def _choose_color(self, key: str) -> None:
        """Open a validated Qt color picker for one setting.

        Args:
            key: Semantic color key.
        """
        selected = QColorDialog.getColor(
            QColor(self._config.colors[key]), self, COLOR_LABELS[key]
        )
        if not selected.isValid():
            return
        self._config.set_color(key, selected.name())
        self._update_button(key)
        self._changed()

    def _save(self) -> None:
        """Save settings and notify renderers of the persisted values."""
        self._config.save()
        self._changed()
