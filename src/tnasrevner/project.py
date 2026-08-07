"""Versioned, portable persistence model for minimal Tnasrevner projects."""

# pylint: disable=duplicate-code

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from collections.abc import Callable
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import shutil
from typing import Any, BinaryIO
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

from .kicad import KiCadFormatError, parse_footprint

CURRENT_FORMAT_VERSION = 2
PROJECT_FILENAME = "project.json"
PROJECT_ARCHIVE_SUFFIX = ".revp"
PROJECT_ARCHIVE_MAGIC = b"REVP"
PROJECT_ARCHIVE_HEADER = PROJECT_ARCHIVE_MAGIC + f"{CURRENT_FORMAT_VERSION:04d}".encode(
    "ascii"
)
_ARCHIVE_HEADER_SIZE = len(PROJECT_ARCHIVE_HEADER)
_SIDES = frozenset({"top", "bottom"})
_DISPLAY_MODES = frozenset(
    {
        "top",
        "bottom",
        "side_by_side",
        "both",
        "nets",
        "net_summary",
        "bom",
        "schematic",
    }
)
_PAD_SHAPES = frozenset({"rect", "circle", "oval", "roundrect", "trapezoid"})


class ProjectFormatError(ValueError):
    """Raised when project data is missing, corrupt, or unsupported."""


def _archive_payload_offset(stream: BinaryIO) -> int:
    """Validate archive prefix and return ZIP payload offset.

    Args:
        stream: Open binary project stream positioned at its beginning.

    Returns:
        Byte offset where the ZIP payload starts; zero for legacy raw ZIP files.

    Raises:
        ProjectFormatError: If prefix is missing, malformed, or unsupported.
    """
    prefix = stream.read(_ARCHIVE_HEADER_SIZE)
    if prefix.startswith(b"PK"):
        stream.seek(0)
        return 0
    if len(prefix) != _ARCHIVE_HEADER_SIZE:
        raise ProjectFormatError("invalid or truncated REVP archive header")
    if prefix[: len(PROJECT_ARCHIVE_MAGIC)] != PROJECT_ARCHIVE_MAGIC:
        raise ProjectFormatError("missing REVP archive header")
    version_bytes = prefix[len(PROJECT_ARCHIVE_MAGIC) :]
    if not version_bytes.isdigit() or len(version_bytes) != 4:
        raise ProjectFormatError("malformed REVP archive header version")
    version = int(version_bytes)
    if version not in {1, CURRENT_FORMAT_VERSION}:
        raise ProjectFormatError(f"unsupported REVP archive version: {version}")
    return _ARCHIVE_HEADER_SIZE


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
class ImageAsset:  # pylint: disable=too-many-instance-attributes
    """An imported image with original source and replayable transformations."""

    side: str
    path: str
    original_name: str
    pixels_per_mm: float | None = None
    original_path: str | None = None
    calibration_line: tuple[float, float, float, float] | None = None
    calibration_length_mm: float | None = None
    transformations: tuple[tuple[float, tuple[float, float, float, float]], ...] = ()

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
        for rotation, crop in self.transformations:
            if not isinstance(rotation, (int, float)) or not math.isfinite(rotation):
                raise ProjectFormatError("image rotation must be a finite number")
            if len(crop) != 4 or not all(
                isinstance(value, (int, float)) and math.isfinite(value)
                for value in crop
            ):
                raise ProjectFormatError("image crop must have four numbers")
            x, y, width, height = crop
            if (  # pylint: disable=too-many-boolean-expressions
                x < 0
                or y < 0
                or width <= 0
                or height <= 0
                or x + width > 1
                or y + height > 1
            ):
                raise ProjectFormatError("image crop must fit between 0 and 1")

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
            "transformations": [
                {"rotation": rotation, "crop": crop}
                for rotation, crop in self.transformations
            ],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ImageAsset":
        """Build an image asset from validated JSON-like data."""
        if not isinstance(data, dict):
            raise ProjectFormatError("image asset must be an object")
        transformations = data.get("transformations", [])
        if not isinstance(transformations, list):
            raise ProjectFormatError("image transformations must be an array")
        parsed_transformations = []
        for transformation in transformations:
            if not isinstance(transformation, dict):
                raise ProjectFormatError("image transformation must be an object")
            crop = transformation.get("crop")
            if not isinstance(crop, (list, tuple)):
                raise ProjectFormatError("image crop must be an array")
            parsed_transformations.append(
                (transformation.get("rotation", 0.0), tuple(crop))
            )
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
            transformations=tuple(parsed_transformations),
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
    function: str = ""
    schematic_x: float | None = None
    schematic_y: float | None = None
    schematic_glued: bool = False

    def __post_init__(self) -> None:
        _required_string(self.name, "pad name")
        _required_string(self.pad_id, "pad id")
        if self.side not in _SIDES:
            raise ProjectFormatError("pad side must be 'top' or 'bottom'")
        if self.net is not None:
            _required_string(self.net, "pad net")
        if not isinstance(self.function, str):
            raise ProjectFormatError("pad function must be a string")
        if (self.schematic_x is None) != (self.schematic_y is None):
            raise ProjectFormatError("pad schematic position must contain x and y")
        if self.schematic_x is not None and not all(
            isinstance(value, (int, float))
            for value in (self.schematic_x, self.schematic_y)
        ):
            raise ProjectFormatError("pad schematic coordinates must be numbers")
        if not isinstance(self.schematic_glued, bool):
            raise ProjectFormatError("pad schematic glued flag must be boolean")
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
            "function": self.function,
            "schematic_x": self.schematic_x,
            "schematic_y": self.schematic_y,
            "schematic_glued": self.schematic_glued,
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
            function=data.get("function", ""),
            schematic_x=data.get("schematic_x"),
            schematic_y=data.get("schematic_y"),
            schematic_glued=data.get("schematic_glued", False),
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


def _default_component_pin_function(
    footprint_library: str,
    footprint_name: str,
    reference: str,
    pin: "ComponentPin",
) -> str:
    """Provide a visible fallback function when legacy data left it empty."""
    family = footprint_library.split("_", maxsplit=1)[0].casefold()
    footprint = footprint_name.casefold()
    if family == "capacitor" or "capacitor" in footprint:
        if any(word in footprint for word in ("electro", "polar", "cp")):
            return "+" if pin.number in {"1", "+"} else "-"
        return "CNX"
    if reference.casefold().startswith("u"):
        return pin.net_id or f"Pin {pin.number}"
    return pin.net_id or f"Pin {pin.number}"


@dataclass(frozen=True)
class ComponentPin:
    """Logical component pin mapped to one physical footprint pad."""

    number: str
    pin_id: str
    function: str = ""
    footprint_pad: str | None = None
    net_id: str | None = None

    def __post_init__(self) -> None:
        _required_string(self.number, "component pin number")
        _required_string(self.pin_id, "component pin id")
        if not isinstance(self.function, str):
            raise ProjectFormatError("component pin function must be a string")
        if self.footprint_pad is not None:
            _required_string(self.footprint_pad, "component footprint pad")
        if self.net_id is not None:
            _required_string(self.net_id, "component pin net id")

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible component pin data."""
        return {
            "number": self.number,
            "pin_id": self.pin_id,
            "function": self.function,
            "footprint_pad": self.footprint_pad,
            "net_id": self.net_id,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ComponentPin":
        """Build a component pin from validated JSON-like data."""
        if not isinstance(data, dict):
            raise ProjectFormatError("component pin must be an object")
        return cls(
            number=_required_string(data.get("number"), "component pin number"),
            pin_id=_required_string(data.get("pin_id"), "component pin id"),
            function=data.get("function", ""),
            footprint_pad=data.get("footprint_pad"),
            net_id=data.get("net_id"),
        )


@dataclass(frozen=True)
class FootprintDefinition:
    """One reusable KiCad footprint source shared by component instances."""

    definition_id: str
    library: str
    name: str
    path: str
    source_revision: str
    content_hash: str

    def __post_init__(self) -> None:
        _required_string(self.definition_id, "footprint definition id")
        _required_string(self.library, "footprint definition library")
        _required_string(self.name, "footprint definition name")
        _relative_asset_path(self.path)
        _required_string(self.source_revision, "footprint definition source_revision")
        if len(self.content_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.content_hash
        ):
            raise ProjectFormatError(
                "footprint definition content_hash must be SHA-256"
            )

    @staticmethod
    def identity(library: str, name: str, content: bytes) -> str:
        """Return a stable identity including metadata and source bytes."""
        digest = hashlib.sha256()
        digest.update(library.encode("utf-8"))
        digest.update(b"\0")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        return f"fp-{digest.hexdigest()}"

    @staticmethod
    def hash_content(content: bytes) -> str:
        """Return the SHA-256 digest used to validate archived source bytes."""
        return hashlib.sha256(content).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible definition data."""
        return {
            "definition_id": self.definition_id,
            "library": self.library,
            "name": self.name,
            "path": self.path,
            "source_revision": self.source_revision,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "FootprintDefinition":
        """Build a footprint definition from validated JSON-like data."""
        if not isinstance(data, dict):
            raise ProjectFormatError("footprint definition must be an object")
        return cls(
            definition_id=_required_string(
                data.get("definition_id"), "footprint definition id"
            ),
            library=_required_string(
                data.get("library"), "footprint definition library"
            ),
            name=_required_string(data.get("name"), "footprint definition name"),
            path=_relative_asset_path(data.get("path")),
            source_revision=_required_string(
                data.get("source_revision"), "footprint definition source_revision"
            ),
            content_hash=_required_string(
                data.get("content_hash"), "footprint definition content_hash"
            ),
        )


@dataclass(frozen=True)
class Device:  # pylint: disable=too-many-instance-attributes
    """A KiCad component/footprint instance placed on one board-side image."""

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
    value: str = ""
    pins: list[ComponentPin] = field(default_factory=list)
    schematic_x: float | None = None
    schematic_y: float | None = None
    schematic_rotation: float = 0.0
    schematic_glued: bool = False
    object_type: str = ""
    description: str = ""
    note: str = ""
    datasheet: str = ""
    footprint_definition_id: str | None = None

    def __post_init__(self) -> None:  # pylint: disable=too-many-branches
        _required_string(self.reference, "device reference")
        if len(self.reference) > 64:
            raise ProjectFormatError("device reference is too long")
        _required_string(self.device_id, "device id")
        _required_string(self.footprint_library, "device footprint_library")
        _required_string(self.footprint_name, "device footprint_name")
        _relative_asset_path(self.footprint_path)
        _required_string(self.source_revision, "device source_revision")
        if not isinstance(self.value, str):
            raise ProjectFormatError("device value must be a string")
        if (self.schematic_x is None) != (self.schematic_y is None):
            raise ProjectFormatError("schematic position must contain x and y")
        if self.schematic_x is not None and not all(
            isinstance(value, (int, float))
            for value in (self.schematic_x, self.schematic_y)
        ):
            raise ProjectFormatError("schematic position must be numeric")
        if not isinstance(self.schematic_rotation, (int, float)):
            raise ProjectFormatError("schematic rotation must be numeric")
        if not isinstance(self.schematic_glued, bool):
            raise ProjectFormatError("schematic glued flag must be boolean")
        if not isinstance(self.object_type, str):
            raise ProjectFormatError("device object type must be a string")
        if not isinstance(self.description, str):
            raise ProjectFormatError("device description must be a string")
        if not isinstance(self.note, str):
            raise ProjectFormatError("device note must be a string")
        if not isinstance(self.datasheet, str):
            raise ProjectFormatError("device datasheet must be a string")
        if self.footprint_definition_id is not None and not isinstance(
            self.footprint_definition_id, str
        ):
            raise ProjectFormatError("device footprint_definition_id must be a string")
        if not all(isinstance(pin, ComponentPin) for pin in self.pins):
            raise ProjectFormatError("device pins must be component pin objects")
        pin_numbers = [pin.number for pin in self.pins]
        if len(set(pin_numbers)) != len(pin_numbers):
            raise ProjectFormatError("component pin numbers must be unique")
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
            "footprint_definition_id": self.footprint_definition_id,
            "value": self.value,
            "pins": [pin.to_dict() for pin in self.pins],
            "schematic_x": self.schematic_x,
            "schematic_y": self.schematic_y,
            "schematic_rotation": self.schematic_rotation,
            "schematic_glued": self.schematic_glued,
            "object_type": self.object_type,
            "description": self.description,
            "note": self.note,
            "datasheet": self.datasheet,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Device":
        """Build a device from validated JSON-like data."""
        if not isinstance(data, dict):
            raise ProjectFormatError("device must be an object")
        pins = [ComponentPin.from_dict(pin) for pin in data.get("pins", [])]
        reference = _required_string(data.get("reference"), "device reference")
        footprint_library = _required_string(
            data.get("footprint_library"), "device footprint_library"
        )
        footprint_name = _required_string(
            data.get("footprint_name"), "device footprint_name"
        )
        pins = [
            replace(
                pin,
                function=pin.function
                or _default_component_pin_function(
                    footprint_library, footprint_name, reference, pin
                ),
            )
            for pin in pins
        ]
        return cls(
            device_id=_required_string(data.get("device_id"), "device id"),
            reference=reference,
            side=_required_string(data.get("side"), "device side"),
            x=data.get("x"),
            y=data.get("y"),
            rotation=data.get("rotation", 0.0),
            footprint_library=footprint_library,
            footprint_name=footprint_name,
            footprint_path=_relative_asset_path(data.get("footprint_path")),
            source_revision=_required_string(
                data.get("source_revision"), "device source_revision"
            ),
            value=data.get("value", ""),
            pins=pins,
            schematic_x=data.get("schematic_x"),
            schematic_y=data.get("schematic_y"),
            schematic_rotation=data.get("schematic_rotation", 0.0),
            schematic_glued=data.get("schematic_glued", False),
            object_type=data.get("object_type", ""),
            description=data.get("description", ""),
            note=data.get("note", ""),
            datasheet=data.get("datasheet", ""),
            footprint_definition_id=data.get("footprint_definition_id"),
        )


def swap_two_pin_assignments(
    device: Device, pads: list[Pad]
) -> tuple[Device, list[Pad]]:
    """Exchange electrical assignments for one two-pad device.

    Args:
        device: Device whose two physical terminals are being exchanged.
        pads: Generated pads belonging to ``device``.

    Returns:
        Updated device rotated by 180 degrees and updated generated pads.

    Raises:
        ValueError: If exactly two generated pads and matching component pins
            are not available.
    """
    if len(pads) != 2 or any(pad.number is None for pad in pads):
        raise ValueError("two generated numbered pads required")
    numbers = {pad.number for pad in pads}
    pins = [pin for pin in device.pins if pin.number in numbers]
    if len(pins) != 2 or {pin.number for pin in pins} != numbers:
        raise ValueError("two matching component pins required")
    first, second = pins
    first_pad, second_pad = pads
    updated_pins = [
        (
            replace(
                pin,
                pin_id=second.pin_id,
                function=second.function,
                net_id=second.net_id,
            )
            if pin.number == first.number
            else (
                replace(
                    pin,
                    pin_id=first.pin_id,
                    function=first.function,
                    net_id=first.net_id,
                )
                if pin.number == second.number
                else pin
            )
        )
        for pin in device.pins
    ]
    updated_pads = [
        replace(first_pad, net=second_pad.net, function=second_pad.function),
        replace(second_pad, net=first_pad.net, function=first_pad.function),
    ]
    updated_device = replace(
        device,
        rotation=(device.rotation + 180.0) % 360.0,
        pins=updated_pins,
    )
    return updated_device, updated_pads


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


@dataclass(frozen=True)
class Net:
    """Stable electrical net identity with an editable display name."""

    net_id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""

    def __post_init__(self) -> None:
        _required_string(self.net_id, "net id")
        _required_string(self.name, "net name")

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible net data."""
        return {"net_id": self.net_id, "name": self.name}

    @classmethod
    def from_dict(cls, data: Any) -> "Net":
        """Build a net from validated JSON-like data."""
        if not isinstance(data, dict):
            raise ProjectFormatError("net must be an object")
        return cls(
            net_id=_required_string(data.get("net_id"), "net id"),
            name=_required_string(data.get("name"), "net name"),
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
    nets: list[Net] = field(default_factory=list)
    footprint_definitions: list[FootprintDefinition] = field(default_factory=list)
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
        definition_ids = [item.definition_id for item in self.footprint_definitions]
        if len(set(definition_ids)) != len(definition_ids):
            raise ProjectFormatError("footprint definition ids must be unique")
        definitions = set(definition_ids)
        for device in self.devices:
            if device.footprint_definition_id is not None and (
                device.footprint_definition_id not in definitions
            ):
                raise ProjectFormatError(
                    "device references an unknown footprint definition"
                )
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
        net_names = {
            net
            for net in (
                [pad.net for pad in self.pads]
                + [pin.net_id for device in self.devices for pin in device.pins]
            )
            if net
        }
        nets_by_name: dict[str, Net] = {}
        for net in self.nets:
            key = net.name.casefold()
            if key in nets_by_name:
                raise ProjectFormatError("net names must be unique")
            nets_by_name[key] = net
        for name in sorted(net_names):
            if name.casefold() not in nets_by_name:
                generated = Net(name=name)
                self.nets.append(generated)
                nets_by_name[name.casefold()] = generated
        net_ids = [net.net_id for net in self.nets]
        if len(set(net_ids)) != len(net_ids):
            raise ProjectFormatError("net ids must be unique")

    def reassign_device_footprint(self, device_id: str, definition_id: str) -> None:
        """Associate one device with another shared footprint definition."""
        definition = next(
            (
                item
                for item in self.footprint_definitions
                if item.definition_id == definition_id
            ),
            None,
        )
        if definition is None:
            raise ProjectFormatError(f"unknown footprint definition: {definition_id}")
        for index, device in enumerate(self.devices):
            if device.device_id == device_id:
                self.devices[index] = replace(
                    device,
                    footprint_library=definition.library,
                    footprint_name=definition.name,
                    footprint_path=definition.path,
                    source_revision=definition.source_revision,
                    footprint_definition_id=definition.definition_id,
                )
                return
        raise ProjectFormatError(f"unknown device: {device_id}")

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
            "nets": [net.to_dict() for net in self.nets],
            "footprint_definitions": [
                definition.to_dict() for definition in self.footprint_definitions
            ],
            "display": self.display.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ProjectDocument":
        """Build a project from JSON-like data and reject invalid input."""
        if not isinstance(data, dict):
            raise ProjectFormatError("project root must be an object")
        version = data.get("format_version")
        if version not in {1, CURRENT_FORMAT_VERSION}:
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
        nets = data.get("nets", [])
        if not isinstance(nets, list):
            raise ProjectFormatError("nets must be an array")
        footprint_definitions = data.get("footprint_definitions", [])
        if not isinstance(footprint_definitions, list):
            raise ProjectFormatError("footprint_definitions must be an array")
        return cls(
            project_id=_required_string(data.get("project_id"), "project_id"),
            project_name=_required_string(data.get("project_name"), "project_name"),
            board_name=_required_string(data.get("board_name"), "board_name"),
            created_at=_required_string(data.get("created_at"), "created_at"),
            updated_at=_required_string(data.get("updated_at"), "updated_at"),
            images=[ImageAsset.from_dict(image) for image in images],
            pads=_pads_from_dict(pads),
            devices=[Device.from_dict(device) for device in devices],
            nets=[Net.from_dict(net) for net in nets],
            footprint_definitions=[
                FootprintDefinition.from_dict(definition)
                for definition in footprint_definitions
            ],
            display=DisplaySettings.from_dict(data.get("display", {})),
            format_version=CURRENT_FORMAT_VERSION,
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

    def save(
        self,
        project: ProjectDocument,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> None:
        """Atomically write project metadata to a directory or `.revp` archive."""
        self.root.mkdir(parents=True, exist_ok=True)
        project.updated_at = _utc_now()
        if self.is_archive:
            self._prune_unused_footprint_assets(project)
            payload_temporary = self.project_file.with_suffix(".revp.zip.tmp")
            temporary = self.project_file.with_suffix(".revp.tmp")
            assets = sorted(self._assets.items())
            total = max(1, len(assets))
            try:
                with ZipFile(
                    payload_temporary, "w", compression=ZIP_DEFLATED
                ) as archive:
                    archive.writestr(
                        PROJECT_FILENAME,
                        json.dumps(project.to_dict(), indent=2, sort_keys=True) + "\n",
                    )
                    if progress is not None:
                        progress("Writing project metadata", 0, total)
                    for index, (path, content) in enumerate(assets, start=1):
                        compression = (
                            ZIP_STORED
                            if path.casefold().endswith(
                                (".png", ".jpg", ".jpeg", ".webp")
                            )
                            else ZIP_DEFLATED
                        )
                        archive.writestr(path, content, compress_type=compression)
                        if progress is not None:
                            progress(f"Writing {path}", index, total)
                with (
                    payload_temporary.open("rb") as payload,
                    temporary.open("wb") as signed_archive,
                ):
                    signed_archive.write(PROJECT_ARCHIVE_HEADER)
                    shutil.copyfileobj(payload, signed_archive)
                temporary.replace(self.project_file)
            finally:
                payload_temporary.unlink(missing_ok=True)
                temporary.unlink(missing_ok=True)
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
        self._migrate_footprints(project)
        self._validate_assets(project)
        return project

    def register_footprint(
        self,
        project: ProjectDocument,
        library: str,
        name: str,
        source_revision: str,
        content: bytes,
    ) -> FootprintDefinition:
        """Register one shared footprint source and archive it once."""
        content_hash = FootprintDefinition.hash_content(content)
        definition_id = FootprintDefinition.identity(library, name, content)
        for definition in project.footprint_definitions:
            if definition.definition_id == definition_id:
                if definition.content_hash != content_hash:
                    raise ProjectFormatError(
                        f"footprint definition hash collision: {definition_id}"
                    )
                self.write_asset(definition.path, content)
                return definition
        definition = FootprintDefinition(
            definition_id=definition_id,
            library=library,
            name=name,
            path=f"assets/kicad/{definition_id}.kicad_mod",
            source_revision=source_revision,
            content_hash=content_hash,
        )
        project.footprint_definitions.append(definition)
        self.write_asset(definition.path, content)
        return definition

    def _migrate_footprints(self, project: ProjectDocument) -> None:
        """Convert direct device assets to shared definitions in memory."""
        source_definitions = list(project.footprint_definitions)
        project.footprint_definitions = []
        definitions: dict[str, FootprintDefinition] = {}
        for source in source_definitions:
            content = self.read_asset(source.path)
            if FootprintDefinition.hash_content(content) != source.content_hash:
                raise ProjectFormatError(f"footprint hash mismatch: {source.path}")
            try:
                footprint = parse_footprint(content, source.library)
            except KiCadFormatError as error:
                raise ProjectFormatError(
                    f"missing or invalid footprint asset: {source.path}"
                ) from error
            if footprint.name != source.name:
                raise ProjectFormatError(f"footprint name mismatch: {source.path}")
            canonical = self.register_footprint(
                project,
                source.library,
                source.name,
                source.source_revision,
                content,
            )
            definitions[source.definition_id] = canonical
        migrated: list[Device] = []
        for device in project.devices:
            definition = definitions.get(device.footprint_definition_id or "")
            if definition is None:
                content = self.read_asset(device.footprint_path)
                try:
                    footprint = parse_footprint(content, device.footprint_library)
                except KiCadFormatError as error:
                    raise ProjectFormatError(
                        f"missing or invalid footprint asset: {device.footprint_path}"
                    ) from error
                if footprint.name != device.footprint_name:
                    raise ProjectFormatError(
                        f"footprint name mismatch: {device.footprint_path}"
                    )
                definition = self.register_footprint(
                    project,
                    device.footprint_library,
                    device.footprint_name,
                    device.source_revision,
                    content,
                )
                definitions[definition.definition_id] = definition
            migrated.append(
                replace(
                    device,
                    footprint_path=definition.path,
                    footprint_definition_id=definition.definition_id,
                )
            )
        project.devices = migrated
        project.format_version = CURRENT_FORMAT_VERSION

    def _prune_unused_footprint_assets(self, project: ProjectDocument) -> None:
        """Drop obsolete per-device footprint copies from archive output."""
        if not project.footprint_definitions:
            return
        paths = {definition.path for definition in project.footprint_definitions}
        self._assets = {
            path: content
            for path, content in self._assets.items()
            if not path.startswith("assets/kicad/") or path in paths
        }

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

    def copy_pending_assets_to(  # pylint: disable=protected-access
        self, target: "ProjectStore"
    ) -> None:
        """Copy in-memory archive assets to another store."""
        target._assets.update(self._assets)

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
                definition = next(
                    item
                    for item in project.footprint_definitions
                    if item.definition_id == device.footprint_definition_id
                )
                if device.footprint_path != definition.path:
                    raise ProjectFormatError("device footprint path mismatch")
                content = self.read_asset(definition.path)
                if FootprintDefinition.hash_content(content) != definition.content_hash:
                    raise ProjectFormatError(
                        f"footprint hash mismatch: {definition.path}"
                    )
                footprint = parse_footprint(content, definition.library)
                if footprint.name != definition.name:
                    raise ProjectFormatError(
                        f"footprint name mismatch: {definition.path}"
                    )
                if (
                    device.footprint_library != definition.library
                    or device.footprint_name != definition.name
                ):
                    raise ProjectFormatError("device footprint metadata mismatch")
            except (ProjectFormatError, KiCadFormatError) as error:
                raise ProjectFormatError(
                    f"missing or invalid footprint asset: {device.footprint_path}"
                ) from error

    def _load_archive(self) -> ProjectDocument:
        try:
            with self.project_file.open("rb") as source:
                offset = _archive_payload_offset(source)
                source.seek(offset)
                with ZipFile(source) as archive:
                    data = json.loads(archive.read(PROJECT_FILENAME).decode("utf-8"))
                    self._assets = {
                        name: archive.read(name)
                        for name in archive.namelist()
                        if name.startswith("assets/") and not name.endswith("/")
                    }
        except ProjectFormatError as error:
            raise ProjectFormatError(f"cannot read project archive: {error}") from error
        except (
            BadZipFile,
            KeyError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
        ) as error:
            raise ProjectFormatError(f"cannot read project archive: {error}") from error
        project = ProjectDocument.from_dict(data)
        self._migrate_footprints(project)
        self._validate_assets(project)
        return project
