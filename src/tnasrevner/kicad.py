"""KiCad footprint cache, catalog, and validated S-expression reader."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import math
from pathlib import Path, PurePosixPath
import shutil
import stat
import re
from typing import Any
from urllib.request import Request, urlopen
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

KICAD_FOOTPRINT_SOURCE = "https://gitlab.com/kicad/libraries/kicad-footprints"
KICAD_FOOTPRINT_REF = "master"
KICAD_SYMBOL_SOURCE = "https://gitlab.com/kicad/libraries/kicad-symbols"
KICAD_SYMBOL_REF = "master"
KICAD_COMMIT_API = (
    "https://gitlab.com/api/v4/projects/"
    "kicad%2Flibraries%2Fkicad-footprints/repository/commits/master"
)
KICAD_ARCHIVE_URL = (
    KICAD_FOOTPRINT_SOURCE + "/-/archive/{revision}/kicad-footprints-{revision}.zip"
)
KICAD_SYMBOL_COMMIT_API = (
    "https://gitlab.com/api/v4/projects/"
    "kicad%2Flibraries%2Fkicad-symbols/repository/commits/master"
)
KICAD_SYMBOL_ARCHIVE_URL = (
    KICAD_SYMBOL_SOURCE + "/-/archive/{revision}/kicad-symbols-{revision}.zip"
)
CACHE_MAX_AGE = timedelta(days=30)
METADATA_FILENAME = "metadata.json"
PAD_INDEX_FILENAME = "pad-count-index.json"
LIBRARIES_DIRECTORY = "libraries"
_MAX_FOOTPRINT_BYTES = 5_000_000
_MAX_ARCHIVE_BYTES = 250_000_000
_MAX_EXTRACTED_BYTES = 500_000_000
_MAX_ARCHIVE_FILES = 100_000
_SUPPORTED_PAD_SHAPES = frozenset({"rect", "circle", "oval", "roundrect", "trapezoid"})
_VISIBLE_GRAPHIC_LAYERS = frozenset({"F.SilkS", "F.Fab"})


class KiCadError(RuntimeError):
    """Base class for actionable KiCad import failures."""


class KiCadCacheError(KiCadError):
    """Raised when no usable footprint cache can be prepared."""


class KiCadFormatError(KiCadError):
    """Raised when a footprint file is malformed or unsupported."""


@dataclass(frozen=True)
class FootprintReference:
    """One footprint selectable from a cached KiCad library."""

    library: str
    name: str
    path: Path

    @property
    def identifier(self) -> str:
        """Return the standard `library:name` identifier."""
        return f"{self.library}:{self.name}"


@dataclass(frozen=True)
class SymbolReference:
    """One KiCad symbol library file in the local symbol cache."""

    library: str
    path: Path

    @property
    def identifier(self) -> str:
        """Return the stable library-file identifier."""
        return f"{self.library}:{self.path.stem}"


@dataclass(frozen=True)
class KiCadSymbolPin:
    """Electrical pin exposed by a KiCad symbol."""

    number: str
    name: str


@dataclass(frozen=True)
class KiCadSymbol:
    """Parsed KiCad symbol with its physical pin numbers and names."""

    library: str
    name: str
    pins: tuple[KiCadSymbolPin, ...]

    @property
    def identifier(self) -> str:
        """Return the stable symbol identifier."""
        return f"{self.library}:{self.name}"


@dataclass(frozen=True)  # pylint: disable=too-many-instance-attributes
class FootprintPad:  # pylint: disable=too-many-instance-attributes
    """Named physical pad geometry in footprint-local millimeters."""

    number: str
    x: float
    y: float
    width: float
    height: float
    shape: str
    rotation: float = 0.0
    pad_type: str = "smd"


@dataclass(frozen=True)
class FootprintGraphic:
    """Simple footprint outline primitive in local millimeters."""

    kind: str
    coordinates: tuple[float, ...]
    width: float = 0.2


@dataclass(frozen=True)
class Footprint:
    """Validated footprint geometry needed by the board workspace."""

    library: str
    name: str
    pads: tuple[FootprintPad, ...]
    graphics: tuple[FootprintGraphic, ...]

    @property
    def identifier(self) -> str:
        """Return the standard `library:name` identifier."""
        return f"{self.library}:{self.name}"

    def pad_count(self) -> int:
        """Return the number of distinct electrical pad numbers."""
        return len({pad.number for pad in self.pads})

    def radius(self) -> float:
        """Return a safe local millimeter radius for drawing a preview."""
        distances = [
            math.hypot(pad.x, pad.y) + math.hypot(pad.width, pad.height) / 2
            for pad in self.pads
        ]
        for graphic in self.graphics:
            coordinates = graphic.coordinates
            if graphic.kind == "circle":
                distances.append(
                    math.hypot(coordinates[0], coordinates[1])
                    + math.hypot(
                        coordinates[2] - coordinates[0],
                        coordinates[3] - coordinates[1],
                    )
                )
                continue
            distances.extend(
                math.hypot(coordinates[index], coordinates[index + 1])
                for index in range(0, len(coordinates), 2)
            )
        return max(distances, default=1.0)

    def dimensions_mm(self) -> tuple[float, float]:
        """Return the footprint bounding-box width and height in millimeters."""
        points: list[tuple[float, float]] = []
        for pad in self.pads:
            angle = math.radians(pad.rotation)
            cosine, sine = math.cos(angle), math.sin(angle)
            for x, y in (
                (-pad.width / 2, -pad.height / 2),
                (-pad.width / 2, pad.height / 2),
                (pad.width / 2, -pad.height / 2),
                (pad.width / 2, pad.height / 2),
            ):
                points.append(
                    (
                        pad.x + x * cosine - y * sine,
                        pad.y + x * sine + y * cosine,
                    )
                )
        for graphic in self.graphics:
            coordinates = graphic.coordinates
            if graphic.kind == "circle":
                radius = (
                    math.hypot(
                        coordinates[2] - coordinates[0],
                        coordinates[3] - coordinates[1],
                    )
                    + graphic.width / 2
                )
                points.extend(
                    (
                        (coordinates[0] - radius, coordinates[1]),
                        (coordinates[0] + radius, coordinates[1]),
                        (coordinates[0], coordinates[1] - radius),
                        (coordinates[0], coordinates[1] + radius),
                    )
                )
                continue
            for index in range(0, len(coordinates), 2):
                points.append(
                    (
                        coordinates[index],
                        coordinates[index + 1],
                    )
                )
        if not points:
            return 0.0, 0.0
        x_values, y_values = zip(*points)
        return max(x_values) - min(x_values), max(y_values) - min(y_values)


@dataclass(frozen=True)  # pylint: disable=too-many-instance-attributes
class PlacedFootprintPad:  # pylint: disable=too-many-instance-attributes
    """One logical KiCad pad transformed into normalized image coordinates."""

    number: str
    x: float
    y: float
    width: float
    height: float
    shape: str
    rotation: float
    side: str = "top"


def generated_pad_name(
    reference: str, device_side: str, placed: PlacedFootprintPad
) -> str:
    """Return a stable name for one physical representation of a device pad.

    Args:
        reference: Device reference designator.
        device_side: Face where the device is placed.
        placed: Transformed physical pad representation.

    Returns:
        Base pad name for the device face, or a face-qualified name for an
        opposite-side through-hole representation.
    """
    base_name = f"{reference}.{placed.number}"
    return base_name if placed.side == device_side else f"{base_name}.{placed.side}"


def mirror_board_rectangle(
    x: float,
    y: float,
    width: float,
    height: float,
    rotation: float,
) -> tuple[float, float, float, float, float]:
    """Flip board geometry around the vertical axis at the right edge.

    This is the physical Top-to-Bottom convention used by the application: the
    board turns like a book cover hinged on its right edge. Vertical position is
    preserved, horizontal position is mirrored, and clockwise rotation changes
    sign when viewed from the opposite face.

    Args:
        x: Normalized rectangle left coordinate.
        y: Normalized rectangle top coordinate.
        width: Normalized rectangle width.
        height: Normalized rectangle height.
        rotation: Clockwise angle in degrees on the source face.

    Returns:
        Mirrored x, y, width, height, and clockwise rotation.
    """
    return 1.0 - x - width, y, width, height, (-rotation) % 360.0


@dataclass(frozen=True)
class CacheResult:
    """Result of preparing the local KiCad footprint cache."""

    root: Path
    revision: str
    refreshed: bool
    warning: str | None = None


class KiCadFootprintCache:
    """Maintain an atomic monthly snapshot of official KiCad footprints."""

    def __init__(
        self,
        root: Path,
        fetch_json: Callable[[str], dict[str, Any]] | None = None,
        fetch_bytes: Callable[[str], bytes] | None = None,
    ) -> None:
        self.root = Path(root)
        self._fetch_json = fetch_json or _download_json
        self._fetch_bytes = fetch_bytes or _download_bytes

    def ensure_ready(self, now: datetime | None = None) -> CacheResult:
        """Reuse a fresh cache or atomically refresh a stale/missing one."""
        now = now or datetime.now(timezone.utc)
        metadata = self._valid_metadata()
        valid_cache = metadata is not None and self._contains_footprints(self.root)
        if valid_cache and self._is_fresh(metadata, now):
            return CacheResult(self.root, metadata["revision"], False)
        try:
            return self._refresh(now)
        except (KiCadCacheError, OSError, ValueError, BadZipFile) as error:
            if valid_cache and metadata is not None:
                return CacheResult(
                    self.root,
                    metadata["revision"],
                    False,
                    f"KiCad cache update failed; using existing cache: {error}",
                )
            raise KiCadCacheError(
                "Cannot download the KiCad footprint library. "
                "Check the network connection and try Add device again. "
                f"Details: {error}"
            ) from error

    def is_ready_and_fresh(self, now: datetime | None = None) -> bool:
        """Return whether no download is currently needed."""
        metadata = self._valid_metadata()
        return bool(
            metadata
            and self._contains_footprints(self.root)
            and self._is_fresh(metadata, now or datetime.now(timezone.utc))
        )

    def current_revision(self) -> str | None:
        """Return the cached source revision when metadata is valid."""
        metadata = self._valid_metadata()
        return metadata["revision"] if metadata else None

    def catalog(self) -> tuple[FootprintReference, ...]:
        """Return all cached footprints sorted by library and name."""
        if not self._contains_footprints(self.root):
            raise KiCadCacheError("KiCad footprint cache is missing or corrupted.")
        references = []
        for path in (self.root / LIBRARIES_DIRECTORY).glob("*.pretty/*.kicad_mod"):
            references.append(FootprintReference(path.parent.stem, path.stem, path))
        return tuple(sorted(references, key=lambda item: item.identifier.lower()))

    def pad_count_index(self) -> dict[str, int] | None:
        """Return the cached pad-count index when it matches the cache revision."""
        metadata = self._valid_metadata()
        if metadata is None:
            return None
        try:
            data = json.loads(
                (self.root / PAD_INDEX_FILENAME).read_text(encoding="utf-8")
            )
            counts = data.get("counts")
            if data.get("revision") != metadata["revision"] or not isinstance(
                counts, dict
            ):
                return None
            return {
                identifier: count
                for identifier, count in counts.items()
                if isinstance(identifier, str) and isinstance(count, int)
            }
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None

    def ensure_pad_count_index(
        self, progress: Callable[[int, int], None] | None = None
    ) -> dict[str, int]:
        """Index pad counts once per downloaded footprint-library revision."""
        cached = self.pad_count_index()
        if cached is not None:
            return cached
        references = self.catalog()
        counts: dict[str, int] = {}
        total = len(references)
        for index, reference in enumerate(references, start=1):
            try:
                counts[reference.identifier] = parse_footprint(
                    reference.path.read_bytes(), reference.library
                ).pad_count()
            except (OSError, KiCadFormatError):
                counts[reference.identifier] = -1
            if progress is not None:
                progress(index, total)
        revision = self.current_revision()
        if revision is None:
            raise KiCadCacheError("Cannot index footprints without a cache revision.")
        self.root.joinpath(PAD_INDEX_FILENAME).write_text(
            json.dumps({"revision": revision, "counts": counts}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return counts

    def load(self, reference: FootprintReference) -> tuple[Footprint, bytes]:
        """Load and validate one selected cached footprint."""
        try:
            path = reference.path.resolve(strict=True)
            library_root = (self.root / LIBRARIES_DIRECTORY).resolve(strict=True)
            path.relative_to(library_root)
            content = path.read_bytes()
        except (OSError, ValueError) as error:
            raise KiCadCacheError(
                f"Cannot read cached footprint {reference.identifier}: {error}"
            ) from error
        return parse_footprint(content, reference.library), content

    def _refresh(self, now: datetime) -> CacheResult:
        commit = self._fetch_json(KICAD_COMMIT_API)
        revision = commit.get("id")
        if (
            not isinstance(revision, str)
            or len(revision) != 40
            or any(character not in "0123456789abcdef" for character in revision)
        ):
            raise KiCadCacheError("KiCad repository returned an invalid revision.")
        archive = self._fetch_bytes(KICAD_ARCHIVE_URL.format(revision=revision))
        if not archive or len(archive) > _MAX_ARCHIVE_BYTES:
            raise KiCadCacheError("KiCad footprint archive has an invalid size.")
        staging = self.root.with_name(f"{self.root.name}.tmp-{uuid4().hex}")
        backup = self.root.with_name(f"{self.root.name}.backup-{uuid4().hex}")
        try:
            staging.mkdir(parents=True)
            self._extract_archive(archive, staging / LIBRARIES_DIRECTORY)
            if not self._contains_footprints(staging):
                raise KiCadCacheError(
                    "Downloaded KiCad archive contains no footprints."
                )
            metadata = {
                "source": KICAD_FOOTPRINT_SOURCE,
                "ref": KICAD_FOOTPRINT_REF,
                "revision": revision,
                "last_successful_update": now.astimezone(timezone.utc).isoformat(),
            }
            (staging / METADATA_FILENAME).write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            if self.root.exists():
                self.root.rename(backup)
            try:
                staging.rename(self.root)
            except OSError:
                if backup.exists() and not self.root.exists():
                    backup.rename(self.root)
                raise
            if backup.exists():
                shutil.rmtree(backup)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return CacheResult(self.root, revision, True)

    @staticmethod
    def _extract_archive(content: bytes, destination: Path) -> None:
        """Safely extract only footprint and license files from a ZIP snapshot."""
        destination.mkdir(parents=True)
        with ZipFile(BytesIO(content)) as archive:
            extracted_size = 0
            extracted_files = 0
            targets: set[Path] = set()
            for item in archive.infolist():
                path = PurePosixPath(item.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise KiCadCacheError("KiCad archive contains an unsafe path.")
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise KiCadCacheError("KiCad archive contains a symbolic link.")
                relative_parts = path.parts[1:]
                if not relative_parts or item.is_dir():
                    continue
                relative = PurePosixPath(*relative_parts)
                is_footprint = (
                    len(relative.parts) == 2
                    and relative.parent.suffix == ".pretty"
                    and relative.suffix == ".kicad_mod"
                )
                is_license = relative.name in {"LICENSE", "LICENSE.md"}
                if not is_footprint and not is_license:
                    continue
                target = destination / Path(*relative.parts)
                extracted_size += item.file_size
                extracted_files += 1
                if (
                    item.file_size > _MAX_FOOTPRINT_BYTES
                    or extracted_size > _MAX_EXTRACTED_BYTES
                    or extracted_files > _MAX_ARCHIVE_FILES
                ):
                    raise KiCadCacheError("KiCad archive exceeds extraction limits.")
                if target in targets:
                    raise KiCadCacheError("KiCad archive contains duplicate files.")
                targets.add(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(item))

    def _valid_metadata(self) -> dict[str, str] | None:
        try:
            data = json.loads(
                (self.root / METADATA_FILENAME).read_text(encoding="utf-8")
            )
            if not isinstance(data, dict):
                return None
            expected = {
                "source": KICAD_FOOTPRINT_SOURCE,
                "ref": KICAD_FOOTPRINT_REF,
            }
            if any(data.get(key) != value for key, value in expected.items()):
                return None
            revision = data.get("revision")
            updated = data.get("last_successful_update")
            if not isinstance(revision, str) or not isinstance(updated, str):
                return None
            datetime.fromisoformat(updated)
            return data
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _contains_footprints(root: Path) -> bool:
        libraries = root / LIBRARIES_DIRECTORY
        return (
            libraries.is_dir()
            and next(libraries.glob("*.pretty/*.kicad_mod"), None) is not None
        )

    @staticmethod
    def _is_fresh(metadata: dict[str, str], now: datetime) -> bool:
        updated = datetime.fromisoformat(metadata["last_successful_update"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return timedelta(0) <= now.astimezone(timezone.utc) - updated < CACHE_MAX_AGE


class KiCadSymbolCache:
    """Maintain a refreshable local snapshot of official KiCad symbols."""

    def __init__(
        self,
        root: Path,
        fetch_json: Callable[[str], dict[str, Any]] | None = None,
        fetch_bytes: Callable[[str], bytes] | None = None,
    ) -> None:
        self.root = Path(root)
        self._fetch_json = fetch_json or _download_json
        self._fetch_bytes = fetch_bytes or _download_bytes

    def ensure_ready(self, now: datetime | None = None) -> CacheResult:
        """Reuse a fresh symbol cache or download a stale snapshot."""
        now = now or datetime.now(timezone.utc)
        metadata = self._metadata()
        valid = metadata is not None and self._contains_symbols(self.root)
        if valid and self._is_fresh(metadata, now):
            return CacheResult(self.root, metadata["revision"], False)
        try:
            commit = self._fetch_json(KICAD_SYMBOL_COMMIT_API)
            revision = commit.get("id")
            if not isinstance(revision, str) or len(revision) != 40:
                raise KiCadCacheError(
                    "KiCad symbol repository returned invalid revision"
                )
            archive = self._fetch_bytes(
                KICAD_SYMBOL_ARCHIVE_URL.format(revision=revision)
            )
            staging = self.root.with_name(f"{self.root.name}.tmp-{uuid4().hex}")
            backup = self.root.with_name(f"{self.root.name}.backup-{uuid4().hex}")
            try:
                staging.mkdir(parents=True)
                self._extract_symbols(archive, staging)
                if not self._contains_symbols(staging):
                    raise KiCadCacheError(
                        "Downloaded KiCad archive contains no symbols"
                    )
                (staging / METADATA_FILENAME).write_text(
                    json.dumps(
                        {
                            "source": KICAD_SYMBOL_SOURCE,
                            "ref": KICAD_SYMBOL_REF,
                            "revision": revision,
                            "last_successful_update": now.astimezone(
                                timezone.utc
                            ).isoformat(),
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                if self.root.exists():
                    self.root.rename(backup)
                staging.rename(self.root)
                if backup.exists():
                    shutil.rmtree(backup)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            return CacheResult(self.root, revision, True)
        except (KiCadCacheError, OSError, ValueError, BadZipFile) as error:
            if valid and metadata is not None:
                return CacheResult(
                    self.root,
                    metadata["revision"],
                    False,
                    f"KiCad symbol update failed; using existing cache: {error}",
                )
            raise KiCadCacheError(
                "Cannot download the KiCad symbol library. "
                f"Check the network connection. Details: {error}"
            ) from error

    def catalog(self) -> tuple[SymbolReference, ...]:
        """Return cached symbol library files sorted by name."""
        if not self._contains_symbols(self.root):
            raise KiCadCacheError("KiCad symbol cache is missing or corrupted")
        return tuple(
            SymbolReference(path.stem, path)
            for path in sorted(self.root.glob("*.kicad_sym"))
        )

    @staticmethod
    def _extract_symbols(content: bytes, destination: Path) -> None:
        """Safely extract top-level KiCad symbol files from an archive."""
        destination.mkdir(parents=True)
        with ZipFile(BytesIO(content)) as archive:
            for item in archive.infolist():
                path = PurePosixPath(item.filename)
                if path.is_absolute() or ".." in path.parts or item.is_dir():
                    continue
                relative = PurePosixPath(*path.parts[1:])
                if len(relative.parts) != 1 or relative.suffix != ".kicad_sym":
                    continue
                if item.file_size > _MAX_FOOTPRINT_BYTES:
                    raise KiCadCacheError("KiCad symbol file is too large")
                (destination / relative.name).write_bytes(archive.read(item))

    @staticmethod
    def _contains_symbols(root: Path) -> bool:
        """Return whether a cache contains at least one symbol file."""
        return next(root.glob("*.kicad_sym"), None) is not None

    def _metadata(self) -> dict[str, str] | None:
        """Read and validate cache metadata."""
        try:
            data = json.loads(
                (self.root / METADATA_FILENAME).read_text(encoding="utf-8")
            )
            if not isinstance(data, dict) or data.get("source") != KICAD_SYMBOL_SOURCE:
                return None
            if not all(
                isinstance(data.get(key), str)
                for key in ("revision", "last_successful_update")
            ):
                return None
            return data
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _is_fresh(metadata: dict[str, str], now: datetime) -> bool:
        """Return whether a symbol cache is younger than the refresh interval."""
        updated = datetime.fromisoformat(metadata["last_successful_update"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return timedelta(0) <= now.astimezone(timezone.utc) - updated < CACHE_MAX_AGE


def _symbol_tokens(content: str) -> list[str]:
    """Tokenize the small S-expression subset needed by KiCad symbols."""
    return re.findall(r'"(?:\\.|[^"\\])*"|[()]|[^()\s]+', content)


def _read_sexpr(tokens: list[str], index: int = 0) -> tuple[list[Any], int]:
    """Read one balanced KiCad S-expression."""
    if index >= len(tokens) or tokens[index] != "(":
        raise KiCadFormatError("KiCad symbol file has malformed S-expression")
    result: list[Any] = []
    index += 1
    while index < len(tokens) and tokens[index] != ")":
        if tokens[index] == "(":
            child, index = _read_sexpr(tokens, index)
            result.append(child)
        else:
            token = tokens[index]
            result.append(token[1:-1] if token.startswith('"') else token)
            index += 1
    if index >= len(tokens):
        raise KiCadFormatError("KiCad symbol file has unbalanced parentheses")
    return result, index + 1


def _symbol_pin_nodes(node: list[Any]) -> list[list[Any]]:
    """Return all nested pin expressions from one symbol expression."""
    pins: list[list[Any]] = []
    for child in node[2:]:
        if isinstance(child, list):
            if child and child[0] == "pin":
                pins.append(child)
            pins.extend(_symbol_pin_nodes(child))
    return pins


def parse_symbol_library(content: bytes, library: str) -> tuple[KiCadSymbol, ...]:
    """Parse KiCad symbols and their pin names from a `.kicad_sym` file."""
    try:
        root, index = _read_sexpr(_symbol_tokens(content.decode("utf-8")))
    except (UnicodeError, KiCadFormatError) as error:
        raise KiCadFormatError(f"Cannot parse KiCad symbol library: {error}") from error
    if index == 0 or not root or root[0] != "kicad_symbol_lib":
        raise KiCadFormatError("KiCad symbol library has an invalid root")
    symbols: list[KiCadSymbol] = []
    for child in root:
        if not isinstance(child, list) or len(child) < 2 or child[0] != "symbol":
            continue
        pins_by_number: dict[str, str] = {}
        for pin in _symbol_pin_nodes(child):
            number = next(
                (
                    item[1]
                    for item in pin
                    if isinstance(item, list)
                    and item[:1] == ["number"]
                    and len(item) > 1
                ),
                None,
            )
            name = next(
                (
                    item[1]
                    for item in pin
                    if isinstance(item, list)
                    and item[:1] == ["name"]
                    and len(item) > 1
                ),
                None,
            )
            if isinstance(number, str) and isinstance(name, str):
                pins_by_number.setdefault(number, name)
        if pins_by_number:
            symbols.append(
                KiCadSymbol(
                    library,
                    str(child[1]),
                    tuple(
                        KiCadSymbolPin(number, name)
                        for number, name in pins_by_number.items()
                    ),
                )
            )
    return tuple(symbols)


def parse_footprint(content: bytes, library: str) -> Footprint:
    """Parse the supported geometry of one KiCad 6+ `.kicad_mod` file."""
    if (
        not isinstance(content, bytes)
        or not content
        or len(content) > _MAX_FOOTPRINT_BYTES
    ):
        raise KiCadFormatError("Footprint file has an invalid size.")
    try:
        text = content.decode("utf-8")
    except UnicodeError as error:
        raise KiCadFormatError("Footprint file is not valid UTF-8.") from error
    root = _parse_sexpression(text)
    if len(root) < 2 or root[0] != "footprint" or not isinstance(root[1], str):
        raise KiCadFormatError("Only KiCad 6+ footprint files are supported.")
    name = root[1].strip()
    if not name:
        raise KiCadFormatError("Footprint name is missing.")
    pads = tuple(pad for node in _children(root, "pad") for pad in _parse_pad(node))
    named_pads = tuple(pad for pad in pads if pad.number)
    if not named_pads:
        raise KiCadFormatError("Footprint contains no named pads.")
    graphics = tuple(_parse_graphics(root))
    return Footprint(library.strip(), name, named_pads, graphics)


def place_footprint_pads(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    footprint: Footprint,
    side: str,
    anchor_x: float,
    anchor_y: float,
    rotation: float,
    image_width: int,
    image_height: int,
    pixels_per_mm: float,
) -> tuple[PlacedFootprintPad, ...]:
    """Transform and group footprint pads for one calibrated image placement."""
    if side not in {"top", "bottom"}:
        raise KiCadFormatError("Device side must be top or bottom.")
    if image_width <= 0 or image_height <= 0 or pixels_per_mm <= 0:
        raise KiCadFormatError("A calibrated image is required for device placement.")
    angle = math.radians(rotation)
    cosine, sine = math.cos(angle), math.sin(angle)
    grouped: dict[tuple[str, str], list[PlacedFootprintPad]] = {}
    for pad in footprint.pads:
        # CMS board images are supplied looking from the component side for
        # both faces, so Bottom uses the same top-view footprint coordinates.
        local_x = pad.x
        local_y = pad.y
        rotated_x = local_x * cosine - local_y * sine
        rotated_y = local_x * sine + local_y * cosine
        center_x = anchor_x + rotated_x * pixels_per_mm / image_width
        center_y = anchor_y + rotated_y * pixels_per_mm / image_height
        width = pad.width * pixels_per_mm / image_width
        height = pad.height * pixels_per_mm / image_height
        pad_rotation = (rotation + pad.rotation) % 360.0
        placed = PlacedFootprintPad(
            pad.number,
            center_x - width / 2,
            center_y - height / 2,
            width,
            height,
            pad.shape,
            pad_rotation,
            side,
        )
        if not _placed_pad_fits(placed):
            raise KiCadFormatError("Footprint pads must fit inside the image.")
        grouped.setdefault((pad.number, side), []).append(placed)
        if pad.pad_type == "thru_hole":
            opposite_side = "bottom" if side == "top" else "top"
            mirrored_geometry = mirror_board_rectangle(
                placed.x,
                placed.y,
                placed.width,
                placed.height,
                placed.rotation,
            )
            mirrored = PlacedFootprintPad(
                pad.number,
                *mirrored_geometry[:4],
                placed.shape,
                mirrored_geometry[4],
                opposite_side,
            )
            if not _placed_pad_fits(mirrored):
                raise KiCadFormatError("Footprint pads must fit inside the image.")
            grouped.setdefault((pad.number, opposite_side), []).append(mirrored)
    result = []
    for (number, pad_side), pads in grouped.items():
        if len(pads) == 1:
            result.append(pads[0])
            continue
        corners = [corner for pad in pads for corner in _placed_pad_corners(pad)]
        left = min(point[0] for point in corners)
        top = min(point[1] for point in corners)
        right = max(point[0] for point in corners)
        bottom = max(point[1] for point in corners)
        result.append(
            PlacedFootprintPad(
                number,
                left,
                top,
                right - left,
                bottom - top,
                "rect",
                0.0,
                pad_side,
            )
        )
    return tuple(result)


def _placed_pad_fits(pad: PlacedFootprintPad) -> bool:
    return (
        0.0 <= pad.x
        and 0.0 <= pad.y
        and pad.x + pad.width <= 1.0
        and pad.y + pad.height <= 1.0
        and all(
            0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in _placed_pad_corners(pad)
        )
    )


def _placed_pad_corners(pad: PlacedFootprintPad) -> tuple[tuple[float, float], ...]:
    center_x = pad.x + pad.width / 2
    center_y = pad.y + pad.height / 2
    cosine = math.cos(math.radians(pad.rotation))
    sine = math.sin(math.radians(pad.rotation))
    corners = []
    for offset_x, offset_y in (
        (-pad.width / 2, -pad.height / 2),
        (pad.width / 2, -pad.height / 2),
        (pad.width / 2, pad.height / 2),
        (-pad.width / 2, pad.height / 2),
    ):
        corners.append(
            (
                center_x + offset_x * cosine - offset_y * sine,
                center_y + offset_x * sine + offset_y * cosine,
            )
        )
    return tuple(corners)


def _download_json(url: str) -> dict[str, Any]:
    content = _download(url, 1_000_000)
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise KiCadCacheError("KiCad repository returned invalid metadata.") from error
    if not isinstance(data, dict):
        raise KiCadCacheError("KiCad repository returned invalid metadata.")
    return data


def _download_bytes(url: str) -> bytes:
    return _download(url, _MAX_ARCHIVE_BYTES)


def _download(url: str, maximum: int) -> bytes:
    request = Request(url, headers={"User-Agent": "Tnasrevner/0.1"})
    try:
        with urlopen(request, timeout=60) as response:  # noqa: S310
            content = response.read(maximum + 1)
    except OSError as error:
        raise KiCadCacheError(str(error)) from error
    if len(content) > maximum:
        raise KiCadCacheError("KiCad download exceeds the allowed size.")
    return content


def _tokenize(text: str) -> Iterator[str]:
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if character == ";":
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline + 1
            continue
        if character in "()":
            yield character
            index += 1
            continue
        if character == '"':
            index += 1
            value = []
            while index < len(text) and text[index] != '"':
                if text[index] == "\\":
                    index += 1
                    if index >= len(text):
                        raise KiCadFormatError("Unterminated footprint string.")
                    escapes = {"n": "\n", "r": "\r", "t": "\t"}
                    value.append(escapes.get(text[index], text[index]))
                else:
                    value.append(text[index])
                index += 1
            if index >= len(text):
                raise KiCadFormatError("Unterminated footprint string.")
            index += 1
            yield "".join(value)
            continue
        end = index
        while end < len(text) and not text[end].isspace() and text[end] not in "()":
            end += 1
        yield text[index:end]
        index = end


def _parse_sexpression(text: str) -> list[Any]:
    stack: list[list[Any]] = []
    roots: list[list[Any]] = []
    for token in _tokenize(text):
        if token == "(":
            stack.append([])
        elif token == ")":
            if not stack:
                raise KiCadFormatError("Unexpected closing parenthesis.")
            node = stack.pop()
            if stack:
                stack[-1].append(node)
            else:
                roots.append(node)
        else:
            if not stack:
                raise KiCadFormatError("Unexpected data outside footprint.")
            stack[-1].append(token)
    if stack or len(roots) != 1:
        raise KiCadFormatError("Footprint has unbalanced S-expression data.")
    return roots[0]


def _children(node: list[Any], name: str) -> Iterator[list[Any]]:
    for child in node:
        if isinstance(child, list) and child and child[0] == name:
            yield child


def _child(node: list[Any], name: str) -> list[Any] | None:
    return next(_children(node, name), None)


def _numbers(node: list[Any] | None, count: int, label: str) -> tuple[float, ...]:
    if node is None or len(node) < count + 1:
        raise KiCadFormatError(f"Footprint {label} is incomplete.")
    try:
        values = tuple(float(node[index]) for index in range(1, count + 1))
    except (TypeError, ValueError) as error:
        raise KiCadFormatError(f"Footprint {label} is invalid.") from error
    if not all(math.isfinite(value) for value in values):
        raise KiCadFormatError(f"Footprint {label} is invalid.")
    return values


def _parse_pad(node: list[Any]) -> tuple[FootprintPad, ...]:
    """Return physical shapes belonging to one KiCad electrical pad."""
    if len(node) < 4 or not all(isinstance(node[index], str) for index in range(1, 4)):
        raise KiCadFormatError("Footprint pad header is invalid.")
    pad_type = node[2]
    shape = node[3]
    if shape not in _SUPPORTED_PAD_SHAPES and shape != "custom":
        raise KiCadFormatError(f"Unsupported KiCad pad shape: {shape}")
    position = _numbers(_child(node, "at"), 2, "pad position")
    size = _numbers(_child(node, "size"), 2, "pad size")
    if size[0] <= 0 or size[1] <= 0:
        raise KiCadFormatError("Footprint pad size must be positive.")
    at = _child(node, "at")
    rotation = _numbers(at, 3, "pad rotation")[2] if at and len(at) >= 4 else 0.0
    if shape == "custom":
        return _parse_custom_pad(
            node,
            node[1].strip(),
            position,
            size,
            rotation,
            pad_type,
        )
    return (
        FootprintPad(
            node[1].strip(), *position, *size, shape, rotation, pad_type
        ),
    )


def _parse_custom_pad(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    node: list[Any],
    number: str,
    position: tuple[float, float],
    size: tuple[float, float],
    rotation: float,
    pad_type: str,
) -> tuple[FootprintPad, ...]:
    """Return custom pad anchor plus polygon primitives as physical shapes."""
    options = _child(node, "options")
    anchor = _child(options, "anchor") if options else None
    anchor_shape = "circle" if anchor and anchor[1:] == ["circle"] else "rect"
    shapes = [
        FootprintPad(number, *position, *size, anchor_shape, rotation, pad_type),
    ]
    primitives = _child(node, "primitives")
    if primitives is not None:
        for polygon in _children(primitives, "gr_poly"):
            point_list = _child(polygon, "pts")
            if point_list is None:
                continue
            points = [
                _numbers(point, 2, "custom pad point")
                for point in _children(point_list, "xy")
            ]
            if points:
                shapes.append(
                    _custom_polygon_pad(number, position, rotation, points, pad_type)
                )
    return tuple(shapes)


def _custom_polygon_pad(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    number: str,
    position: tuple[float, float],
    rotation: float,
    points: list[tuple[float, float]],
    pad_type: str,
) -> FootprintPad:
    """Approximate one custom polygon primitive with its local bounding box."""
    min_x = min(point[0] for point in points)
    max_x = max(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_y = max(point[1] for point in points)
    local_x = (min_x + max_x) / 2
    local_y = (min_y + max_y) / 2
    angle = math.radians(rotation)
    cosine, sine = math.cos(angle), math.sin(angle)
    return FootprintPad(
        number,
        position[0] + local_x * cosine - local_y * sine,
        position[1] + local_x * sine + local_y * cosine,
        max_x - min_x,
        max_y - min_y,
        "rect",
        rotation,
        pad_type,
    )


def _parse_graphics(root: list[Any]) -> Iterator[FootprintGraphic]:
    for kind in ("fp_line", "fp_rect", "fp_circle"):
        for node in _children(root, kind):
            layer = _child(node, "layer")
            if not layer or len(layer) < 2 or layer[1] not in _VISIBLE_GRAPHIC_LAYERS:
                continue
            stroke = _child(node, "stroke")
            width_node = _child(stroke, "width") if stroke else _child(node, "width")
            width = _numbers(width_node, 1, "graphic width")[0] if width_node else 0.2
            if kind in {"fp_line", "fp_rect"}:
                start = _numbers(_child(node, "start"), 2, "graphic start")
                end = _numbers(_child(node, "end"), 2, "graphic end")
                yield FootprintGraphic(kind[3:], start + end, width)
            else:
                center = _numbers(_child(node, "center"), 2, "circle center")
                end = _numbers(_child(node, "end"), 2, "circle end")
                yield FootprintGraphic("circle", center + end, width)
