"""Application-level display configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any

import yaml
from PySide6.QtGui import QColor

LOGGER = logging.getLogger(__name__)

DEFAULT_COLORS = {
    "connected_pad": "#3b82f6",
    "unconnected_pad": "#ff0000",
    "new_connected_pad": "#00ff00",
    "connected_pad_1": "#3b82f6",
    "unconnected_pad_1": "#ffff00",
    "connection_preview": "#66c2ff",
    "connection_line": "#ffffff",
    "selected_terminal": "#bbf7d0",
    "schematic_net": "#e4b363",
    "selected_schematic_net": "#66c2ff",
    "schematic_glued": "#f5a3c7",
    "schematic_pin_text": "#c7d0dc",
}


def contrasting_text_color(background: QColor) -> QColor:
    """Return readable black or white text for a background color.

    Args:
        background: Color behind the text.

    Returns:
        Dark text for light backgrounds, or light text for dark backgrounds.
    """
    red = background.redF()
    green = background.greenF()
    blue = background.blueF()
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return QColor("#111111" if luminance >= 0.55 else "#ffffff")


def _valid_color(value: object) -> str | None:
    """Return a canonical color string, or ``None`` for invalid input.

    Args:
        value: Candidate Qt-compatible color value.

    Returns:
        Canonical hexadecimal color string when valid.
    """
    if not isinstance(value, str):
        return None
    color = QColor(value)
    if not color.isValid():
        return None
    return (
        color.name(QColor.NameFormat.HexArgb)
        if color.alpha() != 255
        else color.name()
    )


@dataclass
class AppConfig:
    """Validated application display preferences stored outside projects."""

    path: Path
    colors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        """Load configuration, retaining defaults for unsafe or missing values.

        Args:
            path: YAML file to read.

        Returns:
            Loaded application configuration.
        """
        config = cls(path)
        if not path.exists():
            return config
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            LOGGER.warning(
                "Could not load application configuration %s: %s", path, error
            )
            return config
        if not isinstance(raw, dict):
            LOGGER.warning("Ignoring non-mapping application configuration %s", path)
            return config
        raw_colors = raw.get("colors", {})
        if isinstance(raw_colors, dict):
            for key, value in raw_colors.items():
                if key not in DEFAULT_COLORS:
                    continue
                color = _valid_color(value)
                if color is None:
                    LOGGER.warning("Ignoring invalid color for %s", key)
                else:
                    config.colors[key] = color
        config.extra = {key: value for key, value in raw.items() if key != "colors"}
        return config

    def save(self) -> bool:
        """Write current preferences while preserving unrelated YAML entries.

        Returns:
            ``True`` when the file was written successfully.
        """
        document = dict(self.extra)
        document["colors"] = dict(self.colors)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
            )
        except (OSError, yaml.YAMLError) as error:
            LOGGER.warning(
                "Could not save application configuration %s: %s", self.path, error
            )
            return False
        return True

    def set_color(self, key: str, value: str) -> bool:
        """Set one supported color after validating it.

        Args:
            key: Semantic color key.
            value: Qt-compatible color string.

        Returns:
            ``True`` if the value was valid and applied.
        """
        if key not in DEFAULT_COLORS:
            return False
        color = _valid_color(value)
        if color is None:
            return False
        self.colors[key] = color
        return True
