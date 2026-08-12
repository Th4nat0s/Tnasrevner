"""Application configuration persistence and validation tests."""

from pathlib import Path

from PySide6.QtGui import QColor
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from tnasrevner.gui import MainWindow
from tnasrevner.lib.app_config import (
    AppConfig,
    DEFAULT_COLORS,
    contrasting_text_color,
)


def test_missing_config_uses_all_defaults(tmp_path: Path) -> None:
    """A missing file produces the complete built-in palette."""
    config = AppConfig.load(tmp_path / "config.yaml")

    assert config.colors == DEFAULT_COLORS


def test_config_round_trip_preserves_colors_and_extra_values(tmp_path: Path) -> None:
    """Saving and loading preserves supported colors and unrelated YAML."""
    path = tmp_path / "config.yaml"
    config = AppConfig.load(path)
    assert config.set_color("connected_pad", "#123456")
    config.extra["future_setting"] = {"enabled": True}
    assert config.save()

    loaded = AppConfig.load(path)
    assert loaded.colors["connected_pad"] == "#123456"
    assert loaded.extra["future_setting"] == {"enabled": True}


def test_invalid_values_fall_back_without_crashing(tmp_path: Path) -> None:
    """Invalid YAML values do not replace validated defaults."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "colors:\n  connected_pad: not-a-color\n  unconnected_pad: '#abcdef'\n",
        encoding="utf-8",
    )

    config = AppConfig.load(path)

    assert config.colors["connected_pad"] == DEFAULT_COLORS["connected_pad"]
    assert config.colors["unconnected_pad"] == QColor("#abcdef").name()


def test_unknown_color_cannot_be_added() -> None:
    """The model rejects unsupported palette keys."""
    config = AppConfig(Path("config.yaml"))

    assert not config.set_color("unused_pad_99", "#ffffff")


def test_contrasting_text_color_handles_light_and_dark_backgrounds() -> None:
    """Configured pad text switches between dark and light for readability."""
    assert contrasting_text_color(QColor("#ffffff")).name() == "#111111"
    assert contrasting_text_color(QColor("#111111")).name() == "#ffffff"


def test_main_window_exposes_config_page_and_loads_yaml(
    tmp_path: Path,
) -> None:
    """The main UI exposes persisted application colors independently of projects."""
    application = QApplication.instance() or QApplication([])
    path = tmp_path / "config.yaml"
    path.write_text("colors:\n  connected_pad: '#123456'\n", encoding="utf-8")
    window = MainWindow(
        show_startup=False,
        settings=QSettings(
            str(tmp_path / "settings.ini"), QSettings.Format.IniFormat
        ),
        config_path=path,
    )

    assert window._tabs.tabText(window._config_tab_index) == "Config"
    assert not window._tabs.isTabVisible(window._config_tab_index)
    application_menu = next(
        menu for menu in window.menuBar().actions() if menu.text() == "Application"
    ).menu()
    assert application_menu is not None
    configuration_action = next(
        action
        for action in application_menu.actions()
        if action.text() == "Configuration…"
    )
    configuration_action.trigger()
    assert window._tabs.currentIndex() == window._config_tab_index
    assert window._tabs.isTabVisible(window._config_tab_index)
    assert window._config_button.accessibleName() == "Configuration"
    assert window._config.colors["connected_pad"] == "#123456"
    window.close()
    application.processEvents()
