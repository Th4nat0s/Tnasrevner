"""Connections, NET, BOM, pad, and routing GUI tests."""

# Qt uses compiled extension modules; test fixtures intentionally inspect GUI
# state to verify lifecycle behavior.
# pylint: disable=wrong-import-position,no-name-in-module,redefined-outer-name
# pylint: disable=unused-argument,protected-access,unused-import,duplicate-code
# pylint: disable=too-many-statements

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QValidator, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QMessageBox,
    QPushButton,
)

from tnasrevner.gui import (
    FootprintPickerDialog,
    ImageEditDialog,
    ImageView,
    MainWindow,
    SchematicCanvas,
    SchematicView,
)
from tnasrevner.kicad import CacheResult, FootprintReference, parse_footprint
from tnasrevner.project import (
    ComponentPin,
    Device,
    ImageAsset,
    Net,
    Pad,
    ProjectDocument,
    ProjectFormatError,
    ProjectStore,
)

FOOTPRINT = b"""(footprint "R_0603"
  (version 20240108) (generator test) (layer "F.Cu")
  (fp_rect (start -1.5 -0.8) (end 1.5 0.8)
    (stroke (width 0.15) (type default)) (fill none) (layer "F.SilkS"))
  (pad "1" smd rect (at -0.8 0) (size 1 1) (layers "F.Cu"))
  (pad "2" smd rect (at 0.8 0) (size 1 1) (layers "F.Cu")))"""


@pytest.fixture(scope="module")
def app() -> QApplication:
    """Provide one headless Qt application for GUI tests."""
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app: QApplication, tmp_path: Path) -> MainWindow:
    """Create isolated main window."""
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    result = MainWindow(show_startup=False, settings=settings)
    yield result
    result._dirty = False
    result.close()


def test_net_view_and_right_click_assignment(window: MainWindow) -> None:
    """Net assignment keeps pad name and appears in the Nets table."""
    window.project = ProjectDocument(
        "Project", "Board", pads=[Pad("P1", "top", 0.1, 0.1, "one", 0.1, 0.1)]
    )
    window._assign_pad_net("one", "GND")
    QApplication.processEvents()
    window._refresh_net_table()

    assert window.project.pads[0].name == "P1"
    assert window.project.pads[0].net == "GND"
    assert window._net_table.item(0, 0).text() == "P1"
    assert window._net_table.item(0, 1).text() == "GND"
    assert not window._net_table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable
    assert not window._net_table.item(0, 1).flags() & Qt.ItemFlag.ItemIsEditable
    assert not window._net_table.item(0, 2).flags() & Qt.ItemFlag.ItemIsEditable
    assert window._net_table.item(0, 3).flags() & Qt.ItemFlag.ItemIsEditable
    assert not window._net_table.item(0, 4).flags() & Qt.ItemFlag.ItemIsEditable

    window._connect_pad_to_net("top", 0.15, 0.15)
    assert window._net_dialog is not None
    window._net_dialog.reject()
    QApplication.processEvents()
    assert window._net_dialog is None


def test_net_summary_renames_every_assignment_and_counts_connections(
    window: MainWindow,
) -> None:
    """NET summary exposes editable names and read-only terminal counts."""
    assert window._tabs.tabText(4) == "Connections"
    assert window._tabs.tabText(5) == "NET"
    device = Device(
        "U1",
        "top",
        0.5,
        0.5,
        "Package_QFP",
        "QFP",
        "assets/kicad/u1.kicad_mod",
        "a" * 40,
        device_id="device",
        pins=[ComponentPin("1", "IO", net_id="GND")],
    )
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad("P1", "top", 0.1, 0.1, "one", net="GND"),
            Pad(
                "U1.1",
                "top",
                0.3,
                0.3,
                "pin-pad",
                net="GND",
                device_id="device",
                number="1",
            ),
        ],
        devices=[device],
    )
    window._refresh_nets_table()

    assert window._nets_table.rowCount() == 1
    assert window._nets_table.item(0, 0).text() == "GND"
    assert window._nets_table.item(0, 1).text() == "3"
    assert window._nets_table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable
    assert not window._nets_table.item(0, 1).flags() & Qt.ItemFlag.ItemIsEditable
    net_id = window._nets_table.item(0, 0).data(Qt.ItemDataRole.UserRole)

    window._nets_table.item(0, 0).setText("GROUND")
    QApplication.processEvents()

    assert [pad.net for pad in window.project.pads] == ["GROUND", "GROUND"]
    assert window.project.devices[0].pins[0].net_id == "GROUND"
    assert window.project.nets[0].net_id == net_id
    assert window.project.nets[0].name == "GROUND"

    window.project.pads = [replace(pad, net=None) for pad in window.project.pads]
    window.project.devices[0].pins[0] = replace(
        window.project.devices[0].pins[0], net_id=None
    )
    window._refresh_nets_table()
    assert window._nets_table.rowCount() == 0
    assert window.project.nets == []


def test_edit_pin_refreshes_connections_function(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing a PIN immediately updates its Connections row."""
    device = Device(
        "U1",
        "top",
        0.5,
        0.5,
        "Package_QFP",
        "QFP",
        "assets/kicad/u1.kicad_mod",
        "a" * 40,
        device_id="device",
        pins=[ComponentPin("1", "OLD", footprint_pad="1")],
    )
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[Pad("U1.1", "top", 0.1, 0.1, "pad", device_id="device", number="1")],
        devices=[device],
    )
    window._refresh_net_table()
    monkeypatch.setattr(
        "tnasrevner.lib.pad_actions.QInputDialog.getText",
        lambda *_args, **_kwargs: ("NEW", True),
    )

    window._edit_component_pin("device", "1")

    assert window._net_table.item(0, 3).text() == "NEW"


def test_disconnect_two_pad_link_cleans_both_ends(window: MainWindow) -> None:
    """Disconnecting a two-pad link removes its lone end and NET registry row."""
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad("P1", "top", 0.1, 0.1, "one", 0.1, 0.1, "GND"),
            Pad("P2", "top", 0.5, 0.5, "two", 0.1, 0.1, "GND"),
        ],
        nets=[Net(name="GND")],
    )
    window._selected_net = "GND"
    window._selected_pad_id = "one"

    window._assign_pad_net("two", "")

    assert all(pad.net is None for pad in window.project.pads)
    assert window.project.nets == []
    assert window._selected_net is None
    assert window._selected_pad_id is None


def test_disconnect_pin_pad_link_cleans_pin_pad_and_net(window: MainWindow) -> None:
    """Trace disconnect clears mirrored component pin, generated pad, and NET."""
    device = Device(
        "U1",
        "top",
        0.5,
        0.5,
        "Package_QFP",
        "QFP",
        "assets/kicad/u1.kicad_mod",
        "a" * 40,
        device_id="device",
        pins=[ComponentPin("1", "IO", footprint_pad="1", net_id="GND")],
    )
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad(
                "U1.1",
                "top",
                0.1,
                0.1,
                "generated",
                net="GND",
                device_id="device",
                number="1",
            ),
            Pad("P1", "top", 0.5, 0.5, "standalone", net="GND"),
        ],
        devices=[device],
        nets=[Net(name="GND")],
    )
    window._selected_net = "GND"
    window._selected_pad_id = "generated"

    window._disconnect_trace("standalone")

    assert all(pad.net is None for pad in window.project.pads)
    assert window.project.devices[0].pins[0].net_id is None
    assert window.project.nets == []
    assert window._selected_net is None


def test_shift_selected_terminals_create_generic_net(window: MainWindow) -> None:
    """Two schematic terminals without nets receive the next automatic net."""
    device = Device(
        "U1",
        "top",
        0.5,
        0.5,
        "Package_QFP",
        "QFP",
        "assets/kicad/u1.kicad_mod",
        "a" * 40,
        device_id="device",
        pins=[ComponentPin("1", "IO", footprint_pad="1")],
    )
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad("U1.1", "top", 0.1, 0.1, "generated", device_id="device", number="1"),
            Pad("P1", "top", 0.3, 0.3, "standalone"),
        ],
        devices=[device],
    )

    window._connect_schematic_terminals(("pin", "device", "1"), ("pin", "device", "1"))
    assert window.project.devices[0].pins[0].net_id is None
    assert window.project.pads[1].net is None

    window._connect_schematic_terminals(
        ("pin", "device", "1"), ("pad", "standalone", None)
    )
    assert {pad.net for pad in window.project.pads} == {"NT1"}


def test_connect_button_links_shift_selected_board_pads_on_release(
    window: MainWindow, tmp_path: Path
) -> None:
    """Shift-click creates rolling links and release stops the preview."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad("P1", "top", 0.1, 0.1, "one", 0.1, 0.1),
            Pad("P2", "top", 0.5, 0.5, "two", 0.1, 0.1),
            Pad("P3", "top", 0.8, 0.8, "three", 0.1, 0.1),
        ],
    )
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0x202020)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.project.images.append(ImageAsset("top", "assets/top.png", "top.png"))
    window._refresh_views()
    window.show()
    QApplication.processEvents()
    view = window._views["top"]
    window._connect_button.click()
    QApplication.processEvents()
    assert window._connection_mode
    prompt = "Connection Mode, Shift to connect, Esc to quit"
    assert window.statusBar().currentMessage() == prompt

    origin = QPoint(
        round(0.15 * (view._label.width() - 1)),
        round(0.15 * (view._label.height() - 1)),
    )
    target = QPoint(
        round(0.55 * (view._label.width() - 1)),
        round(0.55 * (view._label.height() - 1)),
    )
    QTest.keyPress(window, Qt.Key.Key_Shift)
    QTest.mouseClick(
        view._label,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ShiftModifier,
        origin,
    )
    QApplication.processEvents()
    assert view._connection_preview_origin == pytest.approx((0.15, 0.15), abs=0.02)
    pending_color = view._label.pixmap().toImage().pixelColor(15, 15)
    assert pending_color.green() > 200
    assert pending_color.red() < 80
    assert pending_color.blue() < 80
    QTest.mouseMove(
        view._label, QPoint(0.35 * view._label.width(), 0.35 * view._label.height())
    )
    QApplication.processEvents()
    assert view._connection_preview_cursor == pytest.approx((0.35, 0.35), abs=0.02)
    QTest.mouseClick(
        view._label,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ShiftModifier,
        target,
    )
    QApplication.processEvents()
    assert len(window._pending_connection_terminals) == 1
    assert [pad.net for pad in window.project.pads] == ["NT1", "NT1", None]
    assert view._connection_trace_pairs == (
        (
            window.project.pads[0].pad_id,
            window.project.pads[1].pad_id,
        ),
    )
    assert view._connection_preview_origin == pytest.approx((0.55, 0.55), abs=0.02)
    target_two = QPoint(
        round(0.85 * (view._label.width() - 1)),
        round(0.85 * (view._label.height() - 1)),
    )
    QTest.mouseClick(
        view._label,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ShiftModifier,
        target_two,
    )
    QApplication.processEvents()
    assert len(window._pending_connection_terminals) == 1
    assert [pad.net for pad in window.project.pads] == ["NT1", "NT1", "NT1"]
    assert len(view._connection_trace_pairs) == 2
    QTest.keyRelease(window, Qt.Key.Key_Shift)
    QApplication.processEvents()
    assert [pad.net for pad in window.project.pads] == ["NT1", "NT1", "NT1"]
    assert window._selected_net == "NT1"
    assert window._trace_highlight_ids is None
    assert window.statusBar().currentMessage() == prompt
    assert view._connection_preview_origin is None
    assert window._connection_mode
    assert view._connection_trace_pairs is None
    assert view.cursor().shape() == Qt.CursorShape.CrossCursor
    link_color = view._label.pixmap().toImage().pixelColor(35, 35)
    assert min(link_color.red(), link_color.green(), link_color.blue()) > 200

    trace_point = QPoint(
        round(0.35 * (view._label.width() - 1)),
        round(0.35 * (view._label.height() - 1)),
    )
    QTest.mouseClick(view._label, Qt.MouseButton.RightButton, pos=trace_point)
    QApplication.processEvents()
    assert window._pad_menu is not None
    assert [action.text() for action in window._pad_menu.actions()] == ["Disconnect"]
    window._pad_menu.actions()[0].trigger()
    QApplication.processEvents()
    assert [pad.net for pad in window.project.pads] == ["NT1", None, "NT1"]
    assert len(window.project.nets) == 1
    assert window.project.nets[0].name == "NT1"
    assert window._selected_net == "NT1"

    QTest.keyClick(window, Qt.Key.Key_Escape)

    assert [pad.net for pad in window.project.pads] == ["NT1", None, "NT1"]
    assert not window._pending_connection_terminals
    assert not window._connection_mode
    assert view.cursor().shape() != Qt.CursorShape.CrossCursor


def test_connection_mode_plain_pad_click_selects_existing_net(
    window: MainWindow, tmp_path: Path
) -> None:
    """A plain click keeps normal same-net highlighting during Connection Mode."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[Pad("P1", "top", 0.1, 0.1, "one", 0.1, 0.1, "GND")],
        images=[ImageAsset("top", "assets/top.png", "top.png")],
    )
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0x202020)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window._refresh_views()
    window._set_connection_mode(True)

    window._select_pad("top", 0.15, 0.15)

    assert window._selected_net == "GND"
    assert window._selected_pad_id == "one"


def test_multi_terminal_connection_reuses_existing_net_and_advances_nt_names(
    window: MainWindow,
) -> None:
    """A selected existing net wins; otherwise the next NT name is generated."""
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad("P1", "top", 0.1, 0.1, "one", net="GND"),
            Pad("P2", "top", 0.3, 0.1, "two", net="VCC"),
            Pad("P3", "top", 0.5, 0.1, "three"),
            Pad("P4", "top", 0.7, 0.1, "four", net="NT1"),
            Pad("P5", "top", 0.1, 0.3, "five"),
            Pad("P6", "top", 0.3, 0.3, "six"),
        ],
    )

    window._connect_terminals((("pad", "two", None), ("pad", "three", None)))

    assert [pad.net for pad in window.project.pads] == [
        "GND",
        "VCC",
        "VCC",
        "NT1",
        None,
        None,
    ]

    window._connect_terminals(
        (("pad", "one", None), ("pad", "two", None), ("pad", "three", None))
    )
    assert [pad.net for pad in window.project.pads] == [
        "GND",
        "GND",
        "GND",
        "NT1",
        None,
        None,
    ]

    window._connect_terminals((("pad", "five", None), ("pad", "six", None)))
    assert [pad.net for pad in window.project.pads[-2:]] == ["NT2", "NT2"]


def test_connection_session_keeps_endpoint_filter_transient(
    window: MainWindow,
) -> None:
    """A Shift connection highlights only terminals selected in that session."""
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad("P1", "top", 0.1, 0.1, "one"),
            Pad("P2", "top", 0.3, 0.1, "two"),
            Pad("P3", "top", 0.5, 0.1, "three", net="NT1"),
        ],
    )

    window._connect_terminals(
        (("pad", "one", None), ("pad", "two", None))
    )

    assert window._selected_net == "NT2"
    assert window._trace_highlight_ids == frozenset({"one", "two"})

    window._selected_pad_id = None
    window._select_pad("top", 0.55, 0.15)
    assert window._trace_highlight_ids is None


def test_info_exits_connection_mode(window: MainWindow) -> None:
    """Info button cancels Connect mode and clears its selection."""
    window.project = ProjectDocument("Project", "Board")
    window._set_connection_mode(True)
    window._pending_connection_terminals.append(("pad", "missing", None))

    window._show_info()

    assert not window._connection_mode
    assert not window._pending_connection_terminals


def test_net_table_enter_keeps_nets_tab(window: MainWindow) -> None:
    """Committing Net or Function edits must not jump back to Top."""
    device = Device(
        "U1",
        "top",
        0.5,
        0.5,
        "Package_QFP",
        "QFP",
        "assets/kicad/u1.kicad_mod",
        "a" * 40,
        device_id="device",
        pins=[ComponentPin("1", "IO", footprint_pad="1")],
    )
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad("U1.1", "top", 0.1, 0.1, "generated", device_id="device", number="1")
        ],
        devices=[device],
    )
    window._refresh_net_table()
    window._tabs.setCurrentIndex(4)

    window._net_table.item(0, 1).setText("N1")
    QApplication.processEvents()
    assert window._tabs.currentIndex() == 4

    window._net_table.item(0, 3).setText("GPIO")
    QApplication.processEvents()
    assert window._tabs.currentIndex() == 4


def test_pad_refresh_reuses_cached_working_image(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated pad refreshes decode each working image only once."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.project.images.append(ImageAsset("top", "assets/top.png", "top.png"))
    original_read = window.store.read_asset
    reads = 0

    def counted_read(path: str) -> bytes:
        nonlocal reads
        reads += 1
        return original_read(path)

    monkeypatch.setattr(window.store, "read_asset", counted_read)
    window._image_cache.clear()

    window._refresh_views()
    window._refresh_views()

    assert reads == 1


def test_shift_click_pad_menu_disconnects_and_deletes(window: MainWindow) -> None:
    """Pad menu exposes net and deletion actions without a modal loop."""
    window.project = ProjectDocument(
        "Project",
        "Board",
        pads=[Pad("P1", "top", 0.1, 0.1, "one", 0.1, 0.1, "GND")],
    )

    window._show_pad_menu("top", 0.15, 0.15)
    assert window._pad_menu is not None
    actions = {action.text(): action for action in window._pad_menu.actions()}
    assert set(actions) == {"Delete pad", "Connect to net", "Disconnect"}
    assert actions["Disconnect"].isEnabled()
    actions["Disconnect"].trigger()
    assert window.project.pads[0].net is None
    if window._pad_menu is not None:
        window._pad_menu.close()

    window._show_pad_menu("top", 0.15, 0.15)
    assert window._pad_menu is not None
    actions = {action.text(): action for action in window._pad_menu.actions()}
    actions["Delete pad"].trigger()
    assert not window.project.pads
    if window._pad_menu is not None:
        window._pad_menu.close()


def test_shift_click_device_edits_bom_value_and_deletes_whole_footprint(
    window: MainWindow, tmp_path: Path
) -> None:
    """Generated pads expose device actions that update BOM or remove everything."""
    window.store = ProjectStore(tmp_path / "board.revp")
    device = Device(
        "C1",
        "top",
        0.5,
        0.5,
        "Capacitor_SMD",
        "C_0603",
        "assets/kicad/device.kicad_mod",
        "a" * 40,
        device_id="device-id",
    )
    pad = Pad(
        "C1.1",
        "top",
        0.1,
        0.1,
        "pad-id",
        0.1,
        0.1,
        device_id=device.device_id,
        number="1",
    )
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.project = ProjectDocument(
        "Project",
        "Board",
        images=[
            ImageAsset(
                "top",
                "assets/top.png",
                "top.png",
                pixels_per_mm=10,
                calibration_line=(0.0, 0.5, 1.0, 0.5),
                calibration_length_mm=10,
            )
        ],
        pads=[pad],
        devices=[device],
    )
    window.store.write_asset(device.footprint_path, FOOTPRINT)

    window._show_pad_menu("top", 0.5, 0.5)
    assert window._pad_menu is not None
    actions = {action.text(): action for action in window._pad_menu.actions()}
    assert {
        "Delete device",
        "Edit Name…",
        "Set Value…",
        "Edit description…",
        "Edit datasheet…",
    }.issubset(actions)
    actions["Set Value…"].trigger()
    assert window._device_value_dialog is not None
    window._device_value_dialog.setTextValue("100 nF")
    window._device_value_dialog.accept()
    QApplication.processEvents()

    assert window.project.devices[0].value == "100 nF"
    assert window._bom_table.item(0, 0).text() == "C1"
    assert window._bom_table.item(0, 1).text() == "Capacitor"
    assert window._bom_table.item(0, 2).text() == "C_0603"
    assert window._bom_table.item(0, 3).text() == "100 nF"
    if window._pad_menu is not None:
        window._pad_menu.close()

    window._show_pad_menu("top", 0.5, 0.5)
    assert window._pad_menu is not None
    actions = {action.text(): action for action in window._pad_menu.actions()}
    actions["Delete device"].trigger()

    assert not window.project.devices
    assert not window.project.pads
    with pytest.raises(ProjectFormatError):
        window.store.read_asset(device.footprint_path)
    if window._pad_menu is not None:
        window._pad_menu.close()


def test_bom_value_and_object_dropdown_are_editable(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BOM edits values and creates reusable object types through NEW."""
    window.project = ProjectDocument(
        "Project",
        "Board",
        devices=[
            Device(
                "C1",
                "top",
                0.5,
                0.5,
                "Capacitor_SMD",
                "C_0603",
                "assets/kicad/c1.kicad_mod",
                "a" * 40,
            )
        ],
    )
    window._refresh_bom_table()

    assert window._bom_table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable
    assert not window._bom_table.item(0, 1).flags() & Qt.ItemFlag.ItemIsEditable
    assert window._bom_table.item(0, 3).flags() & Qt.ItemFlag.ItemIsEditable
    assert not window._bom_table.item(0, 4).flags() & Qt.ItemFlag.ItemIsEditable
    assert window._bom_table.item(0, 5).flags() & Qt.ItemFlag.ItemIsEditable
    window._bom_table.item(0, 3).setText("100 nF")
    assert window.project.devices[0].value == "100 nF"
    window._bom_table.item(0, 5).setText("https://example.test/datasheet")
    assert window.project.devices[0].datasheet == "https://example.test/datasheet"
    device_id = window.project.devices[0].device_id
    window._bom_table.item(0, 0).setText("C7")
    assert window.project.devices[0].reference == "C7"
    assert window.project.devices[0].device_id == device_id
    assert window.project.pads == []

    monkeypatch.setattr(
        "tnasrevner.lib.bom_tab.QInputDialog.getText",
        lambda *_args, **_kwargs: ("Sensor", True),
    )
    combo = window._bom_table.cellWidget(0, 1)
    assert combo.findText("Transistor") >= 0
    combo.setCurrentText("Transistor")
    QApplication.processEvents()
    assert window.project.devices[0].object_type == "Transistor"
    combo.setCurrentText("NEW")
    QApplication.processEvents()
    assert window.project.devices[0].object_type == "Sensor"


def test_schematic_device_menu_edits_name_with_consistent_wording(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schematic component menu uses the same name editor wording as board menu."""
    device = Device(
        "C1",
        "top",
        0.5,
        0.5,
        "Capacitor_SMD",
        "C_0603",
        "assets/kicad/c1.kicad_mod",
        "a" * 40,
        device_id="device-id",
    )
    window.project = ProjectDocument("Project", "Board", devices=[device])
    captured: dict[str, object] = {}

    class FakeAction:  # pylint: disable=too-few-public-methods
        """Minimal menu action exposing text and identity."""

        def __init__(self, label: str) -> None:
            """Store action label."""
            self._label = label

        def text(self) -> str:
            """Return action label."""
            return self._label

    class FakeMenu:
        """Minimal non-modal menu for testing schematic action selection."""

        def __init__(self, _parent) -> None:
            """Initialize empty action list."""
            self._actions = []

        def addAction(self, label: str) -> FakeAction:  # pylint: disable=invalid-name
            """Add and return one fake action."""
            action = FakeAction(label)
            self._actions.append(action)
            return action

        def actions(self) -> list[FakeAction]:
            """Return fake menu actions."""
            return self._actions

        def exec(self, *_args) -> FakeAction:
            """Return first action as if user selected it."""
            captured["actions"] = [action.text() for action in self._actions]
            return self._actions[0]

    monkeypatch.setattr("tnasrevner.lib.bom_tab.QMenu", FakeMenu)
    monkeypatch.setattr(
        "tnasrevner.lib.bom_tab.QInputDialog.getText",
        lambda _parent, title, prompt, **_kwargs: (
            captured.update(title=title, prompt=prompt) or ("C7", True)
        ),
    )

    window._show_schematic_device_menu(device.device_id)

    assert captured["actions"][0] == "Edit Name…"
    assert captured["title"] == "Edit name"
    assert captured["prompt"] == "Name/reference for C1:"
    assert window.project.devices[0].reference == "C7"


def test_pad_mouse_rectangle_keeps_continuous_placement_mode(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real drag keeps placement armed for the next pad."""
    window.store = ProjectStore(tmp_path / "board.revp")
    window.project = ProjectDocument("Project", "Board")
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    window.store.write_asset(
        "assets/top.png", window._pixmap_bytes(QPixmap.fromImage(image))
    )
    window.project.images.append(ImageAsset("top", "assets/top.png", "top.png"))
    window._views["top"].set_pixmap(QPixmap.fromImage(image))
    window._views["top"].show()
    monkeypatch.setattr(window, "_choose_image_side", lambda: "top")

    window.create_pad()
    assert window._views["top"].cursor().shape() == Qt.CursorShape.CrossCursor
    assert window._views["top"]._label.cursor().shape() == Qt.CursorShape.CrossCursor
    QTest.mousePress(
        window._views["top"]._label, Qt.MouseButton.LeftButton, pos=QPoint(10, 10)
    )
    QTest.mouseMove(window._views["top"]._label, QPoint(40, 40))
    QTest.mouseRelease(
        window._views["top"]._label, Qt.MouseButton.LeftButton, pos=QPoint(40, 40)
    )

    assert len(window.project.pads) == 1
    assert window._views["top"]._pad_placement
    assert window._pending_pad is not None
    assert window._pending_pad.name == "P2"

    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert window._views["top"].cursor().shape() != Qt.CursorShape.CrossCursor
    assert window._views["top"]._label.cursor().shape() != Qt.CursorShape.CrossCursor

    window.create_pad()
    QTest.mousePress(
        window._views["top"]._label,
        Qt.MouseButton.LeftButton,
        pos=QPoint(50, 50),
    )
    QTest.mouseMove(window._views["top"]._label, QPoint(80, 80))
    QTest.mouseRelease(
        window._views["top"]._label,
        Qt.MouseButton.LeftButton,
        pos=QPoint(80, 80),
    )

    assert [pad.name for pad in window.project.pads] == ["P1", "P2"]
