"""Tests for minimal project persistence."""

# Tests use descriptive names; function docstrings add no test value here.
# pylint: disable=missing-function-docstring,duplicate-code

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from tnasrevner.project import (
    ComponentPin,
    Device,
    DisplaySettings,
    FootprintDefinition,
    ImageAsset,
    Net,
    PROJECT_ARCHIVE_HEADER,
    Pad,
    ProjectDocument,
    ProjectFormatError,
    ProjectStore,
)

FOOTPRINT = b"""(footprint "R_0603"
  (version 20240108) (generator test) (layer "F.Cu")
  (pad "1" smd rect (at -0.8 0) (size 1 1) (layers "F.Cu"))
  (pad "2" smd rect (at 0.8 0) (size 1 1) (layers "F.Cu")))"""


def test_empty_project_round_trip(tmp_path: Path) -> None:
    project = ProjectDocument("Project", "Board")
    store = ProjectStore(tmp_path / "project")

    store.save(project)
    loaded = store.load()

    assert loaded.to_dict() == project.to_dict()
    assert store.project_file.is_file()


def test_revp_archive_round_trip_includes_image_bytes(tmp_path: Path) -> None:
    archive = ProjectStore(tmp_path / "board.revp")
    project = ProjectDocument(
        "Project",
        "Board",
        images=[ImageAsset("top", "assets/top.png", "top.png")],
    )
    archive.write_asset("assets/top.png", b"picture-bytes")

    archive.save(project)
    loaded_store = ProjectStore(tmp_path / "board.revp")
    loaded = loaded_store.load()

    assert loaded.to_dict() == project.to_dict()
    assert loaded_store.read_asset("assets/top.png") == b"picture-bytes"


def test_revp_archive_starts_with_signed_header(tmp_path: Path) -> None:
    """Saved archives begin with the fixed current-format REVP header."""
    archive_path = tmp_path / "signed.revp"
    ProjectStore(archive_path).save(ProjectDocument("Project", "Board"))

    assert archive_path.read_bytes()[: len(PROJECT_ARCHIVE_HEADER)] == (
        PROJECT_ARCHIVE_HEADER
    )


def test_legacy_raw_zip_revp_remains_readable(tmp_path: Path) -> None:
    """Existing raw ZIP projects remain readable during header migration."""
    archive_path = tmp_path / "legacy.revp"
    project = ProjectDocument("Project", "Board")
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "project.json", json.dumps(project.to_dict(), sort_keys=True) + "\n"
        )

    assert ProjectStore(archive_path).load().to_dict() == project.to_dict()


@pytest.mark.parametrize(
    ("prefix", "message"),
    (
        (b"", "truncated"),
        (b"REVP", "truncated"),
        (b"NOPE0001", "missing"),
        (b"REVP00A1", "malformed"),
        (b"REVP0003", "unsupported"),
    ),
)
def test_revp_header_errors_are_actionable(
    tmp_path: Path, prefix: bytes, message: str
) -> None:
    """Malformed, truncated, and unsupported headers raise format errors."""
    archive_path = tmp_path / "invalid.revp"
    archive_path.write_bytes(prefix)

    with pytest.raises(ProjectFormatError, match=message):
        ProjectStore(archive_path).load()


def test_revp_archive_keeps_original_and_working_image(tmp_path: Path) -> None:
    archive = ProjectStore(tmp_path / "board.revp")
    project = ProjectDocument(
        "Project",
        "Board",
        images=[
            ImageAsset(
                "top",
                "assets/top.png",
                "photo.jpg",
                10.0,
                "assets/original/top.jpg",
                (0.1, 0.2, 0.8, 0.2),
                30.5,
            )
        ],
    )
    archive.write_asset("assets/top.png", b"cropped")
    archive.write_asset("assets/original/top.jpg", b"original")

    archive.save(project)
    loaded_store = ProjectStore(tmp_path / "board.revp")
    loaded = loaded_store.load()

    assert loaded.images[0].original_path == "assets/original/top.jpg"
    assert loaded.images[0].calibration_line == (0.1, 0.2, 0.8, 0.2)
    assert loaded.images[0].calibration_length_mm == 30.5
    assert loaded_store.read_asset("assets/original/top.jpg") == b"original"


def test_image_scale_is_derived_from_saved_measurement() -> None:
    image = ImageAsset(
        "top",
        "assets/top.png",
        "top.png",
        pixels_per_mm=3.2,
        calibration_line=(0.1, 0.5, 0.9, 0.5),
        calibration_length_mm=40.0,
    )

    assert image.measured_pixels_per_mm(1000, 500) == pytest.approx(20.0)


def test_missing_archive_asset_is_rejected(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "board.revp")
    project = ProjectDocument(
        "Project",
        "Board",
        images=[ImageAsset("top", "assets/top.png", "top.png")],
    )
    store.save(project)

    with pytest.raises(ProjectFormatError, match="missing or unreadable"):
        ProjectStore(tmp_path / "board.revp").load()


def test_project_with_both_images_and_display_round_trip(tmp_path: Path) -> None:
    project = ProjectDocument(
        "Project",
        "Board",
        images=[
            ImageAsset("top", "assets/top.png", "top.png"),
            ImageAsset("bottom", "assets/bottom.jpg", "bottom.jpg"),
        ],
        display=DisplaySettings("side_by_side", 2.5, 12.0, -4.0, False),
    )
    store = ProjectStore(tmp_path / "project.revp")
    store.write_asset("assets/top.png", b"top")
    store.write_asset("assets/bottom.jpg", b"bottom")

    store.save(project)

    assert store.load().images == project.images
    assert store.load().display == project.display


@pytest.mark.parametrize("path", ["/tmp/top.png", "../top.png", "assets\\top.png"])
def test_absolute_or_escaping_asset_paths_are_rejected(path: str) -> None:
    with pytest.raises(ProjectFormatError):
        ImageAsset("top", path, "top.png")


def test_duplicate_image_sides_are_rejected() -> None:
    with pytest.raises(ProjectFormatError):
        ProjectDocument(
            "Project",
            "Board",
            images=[
                ImageAsset("top", "assets/one.png", "one.png"),
                ImageAsset("top", "assets/two.png", "two.png"),
            ],
        )


def test_pad_round_trip_and_validation() -> None:
    """Pads serialize with stable identity and reject invalid coordinates."""
    pad = Pad("P1", "top", 0.25, 0.75, "pad-id", net="GND", function="Power input")
    restored = Pad.from_dict(pad.to_dict())

    assert restored == pad
    with pytest.raises(ProjectFormatError, match="fit between 0 and 1"):
        Pad("bad", "top", 1.1, 0.5)


def test_pad_names_are_unique_but_nets_can_be_shared() -> None:
    """Stable pad labels are unique while one net can connect many pads."""
    project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad("P1", "top", 0.1, 0.1, net="GND"),
            Pad("P2", "top", 0.2, 0.2, net="GND"),
        ],
    )

    assert len(project.pads) == 2
    assert {pad.net for pad in project.pads} == {"GND"}
    with pytest.raises(ProjectFormatError, match="pad names must be unique"):
        ProjectDocument(
            "Project",
            "Board",
            pads=[Pad("P1", "top", 0.1, 0.1), Pad("P1", "bottom", 0.2, 0.2)],
        )


def test_kicad_device_and_generated_pads_round_trip(tmp_path: Path) -> None:
    """Placed devices retain footprint source, rotation, and named pads."""
    device = Device(
        "R1",
        "top",
        0.5,
        0.5,
        "Resistor_SMD",
        "R_0603",
        "assets/kicad/device.kicad_mod",
        "a" * 40,
        "device-id",
        45,
        "10 kΩ",
        schematic_glued=True,
        object_type="Resistor",
    )
    project = ProjectDocument(
        "Project",
        "Board",
        pads=[
            Pad(
                "R1.1",
                "top",
                0.4,
                0.4,
                "pad-id",
                0.1,
                0.1,
                device_id="device-id",
                number="1",
                rotation=45,
            )
        ],
        devices=[device],
    )
    store = ProjectStore(tmp_path / "board.revp")
    store.write_asset(device.footprint_path, FOOTPRINT)

    store.save(project)
    loaded = ProjectStore(tmp_path / "board.revp").load()

    assert loaded.devices[0].reference == device.reference
    assert loaded.devices[0].footprint_library == device.footprint_library
    assert loaded.devices[0].footprint_definition_id is not None
    assert loaded.pads[0].name == "R1.1"
    assert loaded.pads[0].device_id == "device-id"
    assert loaded.pads[0].rotation == 45
    assert loaded.devices[0].value == "10 kΩ"
    assert loaded.devices[0].schematic_glued is True


def test_shared_footprint_definition_deduplicates_legacy_devices(
    tmp_path: Path,
) -> None:
    """Legacy devices with identical source bytes migrate to one definition."""
    first = Device(
        "R1",
        "top",
        0.2,
        0.2,
        "Resistor_SMD",
        "R_0603",
        "assets/kicad/r1.kicad_mod",
        "a" * 40,
        device_id="r1",
    )
    second = Device(
        "R2",
        "top",
        0.7,
        0.7,
        "Resistor_SMD",
        "R_0603",
        "assets/kicad/r2.kicad_mod",
        "a" * 40,
        device_id="r2",
    )
    project = ProjectDocument("Project", "Board", devices=[first, second])
    store = ProjectStore(tmp_path / "shared.revp")
    store.write_asset(first.footprint_path, FOOTPRINT)
    store.write_asset(second.footprint_path, FOOTPRINT)
    store.save(project)

    loaded = ProjectStore(tmp_path / "shared.revp").load()

    assert len(loaded.footprint_definitions) == 1
    assert len({device.footprint_path for device in loaded.devices}) == 1
    assert (
        loaded.devices[0].footprint_definition_id
        == loaded.devices[1].footprint_definition_id
    )

    store = ProjectStore(tmp_path / "shared.revp")
    loaded = store.load()
    store.save(loaded)
    with ZipFile(tmp_path / "shared.revp") as archive:
        footprint_entries = [
            name
            for name in archive.namelist()
            if name.startswith("assets/kicad/") and name.endswith(".kicad_mod")
        ]
    assert len(footprint_entries) == 1


def test_footprint_definition_identity_includes_source_metadata() -> None:
    """Different library metadata cannot silently share a source identity."""
    first = FootprintDefinition.identity("LibraryA", "R_0603", FOOTPRINT)
    second = FootprintDefinition.identity("LibraryB", "R_0603", FOOTPRINT)

    assert first != second


def test_reassigning_footprint_changes_one_device_only() -> None:
    """A definition association is independent for each component instance."""
    first = FootprintDefinition(
        "fp-one",
        "Resistor_SMD",
        "R_0603",
        "assets/kicad/one.kicad_mod",
        "a" * 40,
        FootprintDefinition.hash_content(FOOTPRINT),
    )
    second = FootprintDefinition(
        "fp-two",
        "Resistor_SMD",
        "R_1206",
        "assets/kicad/two.kicad_mod",
        "b" * 40,
        FootprintDefinition.hash_content(FOOTPRINT),
    )
    first_device = Device(
        "R1",
        "top",
        0.2,
        0.2,
        "Resistor_SMD",
        "R_0603",
        first.path,
        first.source_revision,
        device_id="r1",
        footprint_definition_id=first.definition_id,
    )
    second_device = Device(
        "R2",
        "top",
        0.7,
        0.7,
        "Resistor_SMD",
        "R_0603",
        first.path,
        first.source_revision,
        device_id="r2",
        footprint_definition_id=first.definition_id,
    )
    project = ProjectDocument(
        "Project",
        "Board",
        devices=[first_device, second_device],
        footprint_definitions=[first, second],
    )

    project.reassign_device_footprint("r1", "fp-two")

    assert project.devices[0].footprint_definition_id == "fp-two"
    assert project.devices[0].reference == "R1"
    assert project.devices[1].footprint_definition_id == "fp-one"
    assert project.devices[1].x == 0.7


def test_component_pin_identity_function_and_net_round_trip() -> None:
    """Component pins preserve logical mapping separately from physical pads."""
    pin = ComponentPin("1", "GND", "Ground", "1", "GND")
    device = Device(
        "U1",
        "top",
        0.5,
        0.5,
        "Package_QFP",
        "QFP",
        "assets/kicad/u1.kicad_mod",
        "a" * 40,
        pins=[pin],
    )

    restored = Device.from_dict(device.to_dict())

    assert restored.pins == [pin]
    assert restored.pins[0].pin_id == "GND"
    assert restored.pins[0].net_id == "GND"


def test_legacy_component_pin_functions_are_generated_on_load() -> None:
    """Legacy pins with empty functions receive useful load-time fallbacks."""
    data = {
        "device_id": "device-id",
        "reference": "U1",
        "side": "top",
        "x": 0.5,
        "y": 0.5,
        "footprint_library": "Package_QFP",
        "footprint_name": "TQFP-32",
        "footprint_path": "assets/kicad/u1.kicad_mod",
        "source_revision": "a" * 40,
        "pins": [{"number": "1", "pin_id": "1", "net_id": "GND"}],
    }
    loaded = Device.from_dict(data)

    assert loaded.pins[0].function == "GND"


def test_legacy_device_without_value_defaults_to_empty() -> None:
    device = Device(
        "C1",
        "top",
        0.5,
        0.5,
        "Capacitor_SMD",
        "C_0603",
        "assets/kicad/device.kicad_mod",
        "a" * 40,
    )
    data = device.to_dict()
    data.pop("value")

    assert Device.from_dict(data).value == ""


def test_bom_display_mode_is_valid() -> None:
    assert DisplaySettings(mode="bom").mode == "bom"


def test_net_registry_migrates_legacy_assignments_and_preserves_uuid() -> None:
    """Legacy named assignments receive stable persisted NET identities."""
    project = ProjectDocument(
        "Project",
        "Board",
        pads=[Pad("P1", "top", 0.1, 0.1, "one", net="GND")],
    )

    assert len(project.nets) == 1
    assert project.nets[0].name == "GND"
    assert Net.from_dict(project.nets[0].to_dict()) == project.nets[0]


def test_missing_or_malformed_device_footprint_is_rejected(tmp_path: Path) -> None:
    device = Device(
        "U1",
        "top",
        0.5,
        0.5,
        "Package_QFP",
        "QFP",
        "assets/kicad/device.kicad_mod",
        "a" * 40,
    )
    project = ProjectDocument("Project", "Board", devices=[device])
    store = ProjectStore(tmp_path / "board.revp")
    store.write_asset(device.footprint_path, b"broken")
    store.save(project)

    with pytest.raises(ProjectFormatError, match="invalid footprint asset"):
        ProjectStore(tmp_path / "board.revp").load()


def test_legacy_duplicate_pad_names_migrate_to_net() -> None:
    """Legacy same-name pads become unique pads on one shared net."""
    data = ProjectDocument("Project", "Board").to_dict()
    data["pads"] = [
        Pad("GND", "top", 0.1, 0.1, "one").to_dict(),
        Pad("GND", "bottom", 0.2, 0.2, "two").to_dict(),
    ]
    for pad in data["pads"]:
        pad.pop("net")

    project = ProjectDocument.from_dict(data)

    assert [pad.name for pad in project.pads] == ["P1", "P2"]
    assert {pad.net for pad in project.pads} == {"GND"}


def test_unsupported_version_and_corrupt_file_are_rejected(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "project")
    store.root.mkdir()
    store.project_file.write_text('{"format_version": 99}', encoding="utf-8")

    with pytest.raises(ProjectFormatError, match="unsupported"):
        store.load()

    store.project_file.write_text("not json", encoding="utf-8")
    with pytest.raises(ProjectFormatError, match="cannot read project"):
        store.load()


def test_unknown_fields_are_forward_compatible() -> None:
    data = ProjectDocument("Project", "Board").to_dict()
    data["future_field"] = {"value": 1}

    assert ProjectDocument.from_dict(data).project_name == "Project"


def test_project_save_does_not_touch_unrelated_source_file(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    store = ProjectStore(tmp_path / "project")

    store.save(ProjectDocument("Project", "Board"))

    assert source.read_bytes() == b"source"


def test_written_json_is_valid_and_readable(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "project")
    store.save(ProjectDocument("Project", "Board"))

    data = json.loads(store.project_file.read_text(encoding="utf-8"))
    assert data["format_version"] == 2
