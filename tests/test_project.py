"""Tests for minimal project persistence."""

# Tests use descriptive names; function docstrings add no test value here.
# pylint: disable=missing-function-docstring

import json
from pathlib import Path

import pytest

from tnasrevner.project import (
    DisplaySettings,
    ImageAsset,
    ProjectDocument,
    ProjectFormatError,
    ProjectStore,
)


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
    assert data["format_version"] == 1
