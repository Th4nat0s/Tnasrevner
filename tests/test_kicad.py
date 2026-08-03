"""Tests for KiCad footprint parsing, placement, and offline cache behavior."""

# Test names document behavior; function docstrings add no value here.
# pylint: disable=missing-function-docstring

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from tnasrevner.kicad import (
    KICAD_FOOTPRINT_SOURCE,
    KICAD_FOOTPRINT_REF,
    KiCadCacheError,
    KiCadFootprintCache,
    KiCadFormatError,
    place_footprint_pads,
    parse_footprint,
)

REVISION = "a" * 40
FOOTPRINT = b"""(footprint "R_0603_1608Metric"
  (version 20240108)
  (generator tnasrevner_test)
  (layer "F.Cu")
  (fp_rect (start -1.5 -0.8) (end 1.5 0.8)
    (stroke (width 0.15) (type default)) (fill none) (layer "F.SilkS"))
  (fp_line (start -1 0) (end 1 0)
    (stroke (width 0.1) (type default)) (layer "F.Fab"))
  (pad "1" smd roundrect (at -0.8 0) (size 0.9 0.95)
    (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd rect (at 0.8 0 15) (size 0.9 0.95)
    (layers "F.Cu" "F.Paste" "F.Mask")))
"""


def _archive(content: bytes = FOOTPRINT, unsafe: bool = False) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            (
                "../escape.kicad_mod"
                if unsafe
                else "kicad-footprints-test/Resistor_SMD.pretty/R_0603_1608Metric.kicad_mod"
            ),
            content,
        )
        archive.writestr("kicad-footprints-test/LICENSE.md", "license")
    return buffer.getvalue()


def _cache(tmp_path: Path, archive: bytes | None = None) -> KiCadFootprintCache:
    return KiCadFootprintCache(
        tmp_path / "kicad-footprints",
        fetch_json=lambda _url: {"id": REVISION},
        fetch_bytes=lambda _url: archive if archive is not None else _archive(),
    )


def test_parse_kicad_footprint_geometry_and_named_pads() -> None:
    footprint = parse_footprint(FOOTPRINT, "Resistor_SMD")

    assert footprint.identifier == "Resistor_SMD:R_0603_1608Metric"
    assert [pad.number for pad in footprint.pads] == ["1", "2"]
    assert footprint.pads[1].rotation == 15
    assert {graphic.kind for graphic in footprint.graphics} == {"line", "rect"}


@pytest.mark.parametrize(
    "content, message",
    [
        (b"not a footprint", "outside footprint"),
        (b'(module "legacy")', "KiCad 6+"),
        (
            b'(footprint "bad" (pad "1" smd custom (at 0 0) (size 1 1)))',
            "Unsupported",
        ),
        (b'(footprint "empty")', "no named pads"),
    ],
)
def test_reject_malformed_or_unsupported_footprints(
    content: bytes, message: str
) -> None:
    with pytest.raises(KiCadFormatError, match=message):
        parse_footprint(content, "Test")


def test_place_footprint_rotates_without_bottom_mirroring() -> None:
    footprint = parse_footprint(FOOTPRINT, "Resistor_SMD")

    rotated = place_footprint_pads(footprint, "top", 0.5, 0.5, 90, 100, 100, 10)
    bottom = place_footprint_pads(footprint, "bottom", 0.5, 0.5, 0, 100, 100, 10)

    assert rotated[0].y < 0.5 < rotated[1].y
    assert bottom[0].x < 0.5 < bottom[1].x
    assert [pad.number for pad in rotated] == ["1", "2"]


def test_repeated_kicad_pad_number_becomes_one_logical_pad() -> None:
    footprint = parse_footprint(
        b"""(footprint "split-pad" (version 20240108) (generator test)
          (pad "1" smd rect (at -1 0) (size 0.5 0.5) (layers "F.Cu"))
          (pad "1" smd rect (at 1 0) (size 0.5 0.5) (layers "F.Cu")))""",
        "Test",
    )

    pads = place_footprint_pads(footprint, "top", 0.5, 0.5, 0, 100, 100, 10)

    assert len(pads) == 1
    assert pads[0].number == "1"
    assert pads[0].shape == "rect"


def test_first_run_downloads_then_fresh_cache_is_reused(tmp_path: Path) -> None:
    downloads = 0

    def fetch_bytes(_url: str) -> bytes:
        nonlocal downloads
        downloads += 1
        return _archive()

    cache = KiCadFootprintCache(
        tmp_path / "cache",
        fetch_json=lambda _url: {"id": REVISION},
        fetch_bytes=fetch_bytes,
    )
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)

    first = cache.ensure_ready(now)
    second = cache.ensure_ready(now + timedelta(days=29))

    assert first.refreshed
    assert not second.refreshed
    assert downloads == 1
    assert cache.catalog()[0].identifier == "Resistor_SMD:R_0603_1608Metric"
    assert cache.load(cache.catalog()[0])[0].pads[0].number == "1"


def test_stale_cache_updates_after_thirty_days(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    cache.ensure_ready(now)

    result = cache.ensure_ready(now + timedelta(days=30))

    assert result.refreshed


def test_failed_stale_update_uses_valid_offline_cache(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    working = _cache(tmp_path)
    working.ensure_ready(now)
    offline = KiCadFootprintCache(
        working.root,
        fetch_json=lambda _url: (_ for _ in ()).throw(OSError("offline")),
        fetch_bytes=lambda _url: b"",
    )

    result = offline.ensure_ready(now + timedelta(days=31))

    assert not result.refreshed
    assert result.warning and "using existing cache" in result.warning
    assert offline.catalog()


def test_failed_first_download_and_corrupt_cache_are_actionable(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    (root / "metadata.json").write_text(
        json.dumps(
            {
                "source": KICAD_FOOTPRINT_SOURCE,
                "ref": KICAD_FOOTPRINT_REF,
                "revision": REVISION,
                "last_successful_update": "invalid",
            }
        ),
        encoding="utf-8",
    )
    cache = KiCadFootprintCache(
        root,
        fetch_json=lambda _url: (_ for _ in ()).throw(OSError("offline")),
        fetch_bytes=lambda _url: b"",
    )

    with pytest.raises(KiCadCacheError, match="Check the network"):
        cache.ensure_ready()


def test_unsafe_download_does_not_replace_existing_cache(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    cache = _cache(tmp_path)
    cache.ensure_ready(now)
    unsafe = _cache(tmp_path, _archive(unsafe=True))

    result = unsafe.ensure_ready(now + timedelta(days=31))

    assert result.warning
    assert unsafe.catalog()[0].name == "R_0603_1608Metric"
