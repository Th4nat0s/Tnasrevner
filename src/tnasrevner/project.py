"""Versioned, portable persistence model for minimal Tnasrevner projects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

CURRENT_FORMAT_VERSION = 1
PROJECT_FILENAME = "project.json"
_SIDES = frozenset({"top", "bottom"})
_DISPLAY_MODES = frozenset({"top", "bottom", "side_by_side"})


class ProjectFormatError(ValueError):
    """Raised when project data is missing, corrupt, or unsupported."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectFormatError(f"{name} must be a non-empty string")
    return value


def _relative_asset_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectFormatError("image asset path must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ProjectFormatError("image asset path must be relative to project root")
    return value


@dataclass(frozen=True)
class ImageAsset:
    """An imported external-side image referenced by a project-relative path."""

    side: str
    path: str
    original_name: str

    def __post_init__(self) -> None:
        if self.side not in _SIDES:
            raise ProjectFormatError("image side must be 'top' or 'bottom'")
        _relative_asset_path(self.path)
        _required_string(self.original_name, "image original_name")

    def to_dict(self) -> dict[str, str]:
        """Return JSON-compatible image data."""
        return {
            "side": self.side,
            "path": self.path,
            "original_name": self.original_name,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ImageAsset":
        """Build an image asset from validated JSON-like data."""
        if not isinstance(data, dict):
            raise ProjectFormatError("image asset must be an object")
        return cls(
            side=_required_string(data.get("side"), "image side"),
            path=_relative_asset_path(data.get("path")),
            original_name=_required_string(
                data.get("original_name"), "image original_name"
            ),
        )


@dataclass
class DisplaySettings:
    """Display state restored when a project is reopened."""

    mode: str = "top"
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    synchronized: bool = True

    def __post_init__(self) -> None:
        if self.mode not in _DISPLAY_MODES:
            raise ProjectFormatError("display mode is invalid")
        if not all(
            isinstance(value, (int, float))
            for value in (self.zoom, self.pan_x, self.pan_y)
        ):
            raise ProjectFormatError("display coordinates and zoom must be numbers")
        if self.zoom <= 0:
            raise ProjectFormatError("display zoom must be positive")
        if not isinstance(self.synchronized, bool):
            raise ProjectFormatError("display synchronized must be boolean")

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible display data."""
        return {
            "mode": self.mode,
            "zoom": self.zoom,
            "pan_x": self.pan_x,
            "pan_y": self.pan_y,
            "synchronized": self.synchronized,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "DisplaySettings":
        """Build display settings from validated JSON-like data."""
        if not isinstance(data, dict):
            raise ProjectFormatError("display must be an object")
        return cls(
            mode=data.get("mode", "top"),
            zoom=data.get("zoom", 1.0),
            pan_x=data.get("pan_x", 0.0),
            pan_y=data.get("pan_y", 0.0),
            synchronized=data.get("synchronized", True),
        )


@dataclass
class ProjectDocument:  # pylint: disable=too-many-instance-attributes
    """Minimal persisted project state."""

    project_name: str
    board_name: str
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    project_id: str = field(default_factory=lambda: str(uuid4()))
    images: list[ImageAsset] = field(default_factory=list)
    display: DisplaySettings = field(default_factory=DisplaySettings)
    format_version: int = CURRENT_FORMAT_VERSION

    def __post_init__(self) -> None:
        _required_string(self.project_name, "project_name")
        _required_string(self.board_name, "board_name")
        _required_string(self.project_id, "project_id")
        _required_string(self.created_at, "created_at")
        _required_string(self.updated_at, "updated_at")
        if self.format_version != CURRENT_FORMAT_VERSION:
            raise ProjectFormatError(
                f"unsupported project format version: {self.format_version}"
            )
        if len(self.images) > 2 or len({image.side for image in self.images}) != len(
            self.images
        ):
            raise ProjectFormatError("project may contain at most one image per side")

    def to_dict(self) -> dict[str, Any]:
        """Return complete JSON-compatible project data."""
        return {
            "format_version": self.format_version,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "board_name": self.board_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "images": [image.to_dict() for image in self.images],
            "display": self.display.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ProjectDocument":
        """Build a project from JSON-like data and reject invalid input."""
        if not isinstance(data, dict):
            raise ProjectFormatError("project root must be an object")
        version = data.get("format_version")
        if version != CURRENT_FORMAT_VERSION:
            raise ProjectFormatError(f"unsupported project format version: {version}")
        images = data.get("images", [])
        if not isinstance(images, list):
            raise ProjectFormatError("images must be an array")
        return cls(
            format_version=version,
            project_id=_required_string(data.get("project_id"), "project_id"),
            project_name=_required_string(data.get("project_name"), "project_name"),
            board_name=_required_string(data.get("board_name"), "board_name"),
            created_at=_required_string(data.get("created_at"), "created_at"),
            updated_at=_required_string(data.get("updated_at"), "updated_at"),
            images=[ImageAsset.from_dict(image) for image in images],
            display=DisplaySettings.from_dict(data.get("display", {})),
        )


class ProjectStore:
    """Read and write one project directory without touching source images."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def project_file(self) -> Path:
        """Return project metadata file path."""
        return self.root / PROJECT_FILENAME

    def save(self, project: ProjectDocument) -> None:
        """Atomically write project metadata to its project directory."""
        self.root.mkdir(parents=True, exist_ok=True)
        project.updated_at = _utc_now()
        temporary = self.project_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(project.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.project_file)

    def load(self) -> ProjectDocument:
        """Load project metadata, converting file errors to format errors."""
        try:
            data = json.loads(self.project_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProjectFormatError(f"cannot read project: {error}") from error
        return ProjectDocument.from_dict(data)
