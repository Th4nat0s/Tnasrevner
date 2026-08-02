"""Versioned, portable persistence model for minimal Tnasrevner projects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

CURRENT_FORMAT_VERSION = 1
PROJECT_FILENAME = "project.json"
PROJECT_ARCHIVE_SUFFIX = ".revp"
_SIDES = frozenset({"top", "bottom"})
_DISPLAY_MODES = frozenset({"top", "bottom", "side_by_side", "both"})


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
    """Read and write directory projects or single-file `.revp` archives."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._assets: dict[str, bytes] = {}

    @property
    def is_archive(self) -> bool:
        """Whether this store uses the portable single-file format."""
        return self.path.suffix.lower() == PROJECT_ARCHIVE_SUFFIX

    @property
    def root(self) -> Path:
        """Return directory containing the project or archive."""
        return self.path.parent if self.is_archive else self.path

    @property
    def project_file(self) -> Path:
        """Return project metadata file path."""
        return self.path if self.is_archive else self.root / PROJECT_FILENAME

    def save(self, project: ProjectDocument) -> None:
        """Atomically write project metadata to a directory or `.revp` archive."""
        self.root.mkdir(parents=True, exist_ok=True)
        project.updated_at = _utc_now()
        if self.is_archive:
            temporary = self.project_file.with_suffix(".revp.tmp")
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
                archive.writestr(
                    PROJECT_FILENAME,
                    json.dumps(project.to_dict(), indent=2, sort_keys=True) + "\n",
                )
                for path, content in sorted(self._assets.items()):
                    archive.writestr(path, content)
            temporary.replace(self.project_file)
            return
        temporary = self.project_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(project.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.project_file)

    def load(self) -> ProjectDocument:
        """Load project metadata, converting file errors to format errors."""
        if self.is_archive:
            return self._load_archive()
        try:
            data = json.loads(self.project_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProjectFormatError(f"cannot read project: {error}") from error
        return ProjectDocument.from_dict(data)

    def write_asset(self, relative_path: str, content: bytes) -> None:
        """Store asset bytes in the archive or project directory."""
        relative_path = _relative_asset_path(relative_path)
        if not relative_path.startswith("assets/"):
            raise ProjectFormatError("project assets must be stored under assets/")
        if self.is_archive:
            self._assets[relative_path] = content
            return
        destination = self.root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)

    def read_asset(self, relative_path: str) -> bytes:
        """Read asset bytes from the archive or project directory."""
        relative_path = _relative_asset_path(relative_path)
        try:
            if self.is_archive:
                return self._assets[relative_path]
            return (self.root / relative_path).read_bytes()
        except (KeyError, OSError) as error:
            raise ProjectFormatError(f"cannot read asset: {relative_path}") from error

    def _load_archive(self) -> ProjectDocument:
        try:
            with ZipFile(self.project_file) as archive:
                data = json.loads(archive.read(PROJECT_FILENAME).decode("utf-8"))
                self._assets = {
                    name: archive.read(name)
                    for name in archive.namelist()
                    if name.startswith("assets/") and not name.endswith("/")
                }
        except (
            BadZipFile,
            KeyError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
        ) as error:
            raise ProjectFormatError(f"cannot read project archive: {error}") from error
        return ProjectDocument.from_dict(data)
