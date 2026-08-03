"""Versioned, portable persistence model for minimal Tnasrevner projects."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from .kicad import KiCadFormatError, parse_footprint

CURRENT_FORMAT_VERSION = 1
PROJECT_FILENAME = "project.json"
PROJECT_ARCHIVE_SUFFIX = ".revp"
_SIDES = frozenset({"top", "bottom"})
_DISPLAY_MODES = frozenset({"top", "bottom", "side_by_side", "both", "nets"})
_PAD_SHAPES = frozenset({"rect", "circle", "oval", "roundrect", "trapezoid"})


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
        raise ProjectFormatError("asset path must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ProjectFormatError("asset path must be relative to project root")
    return value


@dataclass(frozen=True)
class ImageAsset:
    """An imported image with original and lightweight working versions."""

    side: str
    path: str
    original_name: str
    pixels_per_mm: float | None = None
    original_path: str | None = None
    calibration_line: tuple[float, float, float, float] | None = None
    calibration_length_mm: float | None = None

    def __post_init__(self) -> None:
        if self.side not in _SIDES:
            raise ProjectFormatError("image side must be 'top' or 'bottom'")
        _relative_asset_path(self.path)
        _required_string(self.original_name, "image original_name")
        if self.original_path is not None:
            _relative_asset_path(self.original_path)
        if self.pixels_per_mm is not None and self.pixels_per_mm <= 0:
            raise ProjectFormatError("image pixels_per_mm must be positive")
        if self.calibration_line is not None:
            if len(self.calibration_line) != 4 or not all(
                isinstance(value, (int, float)) and math.isfinite(value)
                for value in self.calibration_line
            ):
                raise ProjectFormatError(
                    "image calibration_line must have four numbers"
                )
        if self.calibration_length_mm is not None and (
            not isinstance(self.calibration_length_mm, (int, float))
            or not math.isfinite(self.calibration_length_mm)
            or self.calibration_length_mm <= 0
        ):
            raise ProjectFormatError(
                "image calibration_length_mm must be a positive number"
            )
        if self.calibration_length_mm is not None and self.calibration_line is None:
            raise ProjectFormatError(
                "image calibration_length_mm requires calibration_line"
            )

    def measured_pixels_per_mm(
        self, image_width: int, image_height: int
    ) -> float | None:
        """Derive physical scale directly from the saved line and real length."""
        if self.calibration_line is None or self.calibration_length_mm is None:
            return None
        start_x, start_y, end_x, end_y = self.calibration_line
        line_pixels = math.hypot(
            (end_x - start_x) * image_width,
            (end_y - start_y) * image_height,
        )
        if line_pixels <= 0:
            return None
        return line_pixels / self.calibration_length_mm

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible image data."""
        return {
            "side": self.side,
            "path": self.path,
            "original_name": self.original_name,
            "pixels_per_mm": self.pixels_per_mm,
            "original_path": self.original_path,
            "calibration_line": self.calibration_line,
            "calibration_length_mm": self.calibration_length_mm,
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
            pixels_per_mm=data.get("pixels_per_mm"),
            original_path=data.get("original_path"),
            calibration_line=(
                tuple(data["calibration_line"])
                if data.get("calibration_line") is not None
                else None
            ),
            calibration_length_mm=data.get("calibration_length_mm"),
        )


@dataclass(frozen=True)
class Pad:  # pylint: disable=too-many-instance-attributes
    """A named rectangular board pad in normalized image coordinates."""

    name: str
    side: str
    x: float
    y: float
    pad_id: str = field(default_factory=lambda: str(uuid4()))
    width: float = 0.02
    height: float = 0.02
    net: str | None = None
    device_id: str | None = None
    number: str | None = None
    shape: str = "rect"
    rotation: float = 0.0

    def __post_init__(self) -> None:
        _required_string(self.name, "pad name")
        _required_string(self.pad_id, "pad id")
        if self.side not in _SIDES:
            raise ProjectFormatError("pad side must be 'top' or 'bottom'")
        if self.net is not None:
            _required_string(self.net, "pad net")
        if (self.device_id is None) != (self.number is None):
            raise ProjectFormatError("device pad requires device_id and number")
        if self.device_id is not None:
            _required_string(self.device_id, "pad device_id")
            _required_string(self.number, "pad number")
        if self.shape not in _PAD_SHAPES:
            raise ProjectFormatError("pad shape is unsupported")
        if not all(
            isinstance(value, (int, float))
            for value in (self.x, self.y, self.width, self.height, self.rotation)
        ):
            raise ProjectFormatError("pad coordinates and rotation must be numbers")
        if (  # pylint: disable=too-many-boolean-expressions
            not 0.0 <= self.x < 1.0
            or not 0.0 <= self.y < 1.0
            or self.width <= 0
            or self.height <= 0
            or self.x + self.width > 1.0
            or self.y + self.height > 1.0
        ):
            raise ProjectFormatError("pad rectangle must fit between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible pad data."""
        return {
            "pad_id": self.pad_id,
            "name": self.name,
            "side": self.side,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "net": self.net,
            "device_id": self.device_id,
            "number": self.number,
            "shape": self.shape,
            "rotation": self.rotation,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Pad":
        """Build a pad from validated JSON-like data."""
        if not isinstance(data, dict):
            raise ProjectFormatError("pad must be an object")
        return cls(
            pad_id=_required_string(data.get("pad_id"), "pad id"),
            name=_required_string(data.get("name"), "pad name"),
            side=_required_string(data.get("side"), "pad side"),
            x=data.get("x"),
            y=data.get("y"),
            width=data.get("width", 0.02),
            height=data.get("height", 0.02),
            net=data.get("net"),
            device_id=data.get("device_id"),
            number=data.get("number"),
            shape=data.get("shape", "rect"),
            rotation=data.get("rotation", 0.0),
        )


def _pads_from_dict(data: list[Any]) -> list[Pad]:
    """Load pads and migrate legacy duplicate names into shared nets."""
    pads = [Pad.from_dict(item) for item in data]
    counts = {pad.name: sum(other.name == pad.name for other in pads) for pad in pads}
    used_names = {pad.name for pad in pads if counts[pad.name] == 1}
    next_index = 1
    migrated: list[Pad] = []
    for item, pad in zip(data, pads):
        if counts[pad.name] == 1 or item.get("net") is not None:
            migrated.append(pad)
            continue
        while f"P{next_index}" in used_names:
            next_index += 1
        name = f"P{next_index}"
        next_index += 1
        used_names.add(name)
        migrated.append(replace(pad, name=name, net=pad.name))
    return migrated


@dataclass(frozen=True)
class Device:  # pylint: disable=too-many-instance-attributes
    """A KiCad footprint instance placed on one board-side image."""

    reference: str
    side: str
    x: float
    y: float
    footprint_library: str
    footprint_name: str
    footprint_path: str
    source_revision: str
    device_id: str = field(default_factory=lambda: str(uuid4()))
    rotation: float = 0.0

    def __post_init__(self) -> None:
        _required_string(self.reference, "device reference")
        if len(self.reference) > 64:
            raise ProjectFormatError("device reference is too long")
        _required_string(self.device_id, "device id")
        _required_string(self.footprint_library, "device footprint_library")
        _required_string(self.footprint_name, "device footprint_name")
        _relative_asset_path(self.footprint_path)
        _required_string(self.source_revision, "device source_revision")
        if self.side not in _SIDES:
            raise ProjectFormatError("device side must be 'top' or 'bottom'")
        if not all(
            isinstance(value, (int, float)) for value in (self.x, self.y, self.rotation)
        ):
            raise ProjectFormatError("device position and rotation must be numbers")
        if not 0.0 <= self.x <= 1.0 or not 0.0 <= self.y <= 1.0:
            raise ProjectFormatError("device position must fit between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible device data."""
        return {
            "device_id": self.device_id,
            "reference": self.reference,
            "side": self.side,
            "x": self.x,
            "y": self.y,
            "rotation": self.rotation,
            "footprint_library": self.footprint_library,
            "footprint_name": self.footprint_name,
            "footprint_path": self.footprint_path,
            "source_revision": self.source_revision,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Device":
        """Build a device from validated JSON-like data."""
        if not isinstance(data, dict):
            raise ProjectFormatError("device must be an object")
        return cls(
            device_id=_required_string(data.get("device_id"), "device id"),
            reference=_required_string(data.get("reference"), "device reference"),
            side=_required_string(data.get("side"), "device side"),
            x=data.get("x"),
            y=data.get("y"),
            rotation=data.get("rotation", 0.0),
            footprint_library=_required_string(
                data.get("footprint_library"), "device footprint_library"
            ),
            footprint_name=_required_string(
                data.get("footprint_name"), "device footprint_name"
            ),
            footprint_path=_relative_asset_path(data.get("footprint_path")),
            source_revision=_required_string(
                data.get("source_revision"), "device source_revision"
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
    pads: list[Pad] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)
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
        pad_ids = [pad.pad_id for pad in self.pads]
        if len(set(pad_ids)) != len(pad_ids):
            raise ProjectFormatError("pad ids must be unique")
        pad_names = [pad.name for pad in self.pads]
        if len(set(pad_names)) != len(pad_names):
            raise ProjectFormatError("pad names must be unique")
        device_ids = [device.device_id for device in self.devices]
        if len(set(device_ids)) != len(device_ids):
            raise ProjectFormatError("device ids must be unique")
        references = [device.reference for device in self.devices]
        if len(set(references)) != len(references):
            raise ProjectFormatError("device references must be unique")
        unknown_devices = {
            pad.device_id
            for pad in self.pads
            if pad.device_id is not None and pad.device_id not in set(device_ids)
        }
        if unknown_devices:
            raise ProjectFormatError("pad references an unknown device")

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
            "pads": [pad.to_dict() for pad in self.pads],
            "devices": [device.to_dict() for device in self.devices],
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
        pads = data.get("pads", [])
        if not isinstance(pads, list):
            raise ProjectFormatError("pads must be an array")
        devices = data.get("devices", [])
        if not isinstance(devices, list):
            raise ProjectFormatError("devices must be an array")
        return cls(
            format_version=version,
            project_id=_required_string(data.get("project_id"), "project_id"),
            project_name=_required_string(data.get("project_name"), "project_name"),
            board_name=_required_string(data.get("board_name"), "board_name"),
            created_at=_required_string(data.get("created_at"), "created_at"),
            updated_at=_required_string(data.get("updated_at"), "updated_at"),
            images=[ImageAsset.from_dict(image) for image in images],
            pads=_pads_from_dict(pads),
            devices=[Device.from_dict(device) for device in devices],
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
        project = ProjectDocument.from_dict(data)
        self._validate_assets(project)
        return project

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

    def remove_asset(self, relative_path: str) -> None:
        """Remove one imported asset from archive or legacy project directory."""
        relative_path = _relative_asset_path(relative_path)
        if self.is_archive:
            self._assets.pop(relative_path, None)
            return
        try:
            (self.root / relative_path).unlink()
        except FileNotFoundError:
            pass

    def _validate_assets(self, project: ProjectDocument) -> None:
        """Reject projects referencing missing, empty, or malformed assets."""
        for image in project.images:
            for path in filter(None, (image.path, image.original_path)):
                try:
                    if not self.read_asset(path):
                        raise ProjectFormatError(f"image asset is empty: {path}")
                except ProjectFormatError as error:
                    raise ProjectFormatError(
                        f"missing or unreadable image asset: {path}"
                    ) from error
        for device in project.devices:
            try:
                content = self.read_asset(device.footprint_path)
                footprint = parse_footprint(content, device.footprint_library)
                if footprint.name != device.footprint_name:
                    raise ProjectFormatError(
                        f"footprint name mismatch: {device.footprint_path}"
                    )
            except (ProjectFormatError, KiCadFormatError) as error:
                raise ProjectFormatError(
                    f"missing or invalid footprint asset: {device.footprint_path}"
                ) from error

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
        project = ProjectDocument.from_dict(data)
        self._validate_assets(project)
        return project
