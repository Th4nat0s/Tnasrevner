# KiCad footprints

Tnasrevner uses the official KiCad footprint repository:

`https://gitlab.com/kicad/libraries/kicad-footprints`

The tracked ref is `master`, currently intended for KiCad 10. Each successful
download records the exact Git commit, source URL, ref, and UTC update time.

## Cache lifecycle

The application prepares the cache after startup without blocking the GUI. It also
checks it before Add Footprint opens. A valid cache younger than 30 days is reused.
At 30 days or older, Tnasrevner attempts an atomic replacement from a new repository
snapshot.

The cache is stored under Qt's application-data directory in `kicad-footprints/`:

- macOS: normally `~/Library/Application Support/Tnasrevner/kicad-footprints`;
- Linux: normally `~/.local/share/Tnasrevner/kicad-footprints`.

If an update fails, an existing valid cache remains available and the application
shows a warning. If the first download fails, Add Footprint reports an actionable error
without changing the project. A corrupt cache can be recovered by removing only the
`kicad-footprints` cache directory and starting Tnasrevner again.

Archives are downloaded into a temporary directory, checked for unsafe paths, and
validated before replacing the current cache. Tests inject local archives and never
require network access.

## Add Footprint workflow

1. Import and calibrate at least one board image.
2. Select **Add Footprint** in Tools.
3. Search for and select a cached KiCad footprint. The picker keeps the five most
   recently selected footprints at the top and previews the highlighted geometry.
4. Accept or edit the suggested unique reference. References follow the selected
   family and increment automatically, for example `C1`, then `C2`.
5. Move the physically scaled preview over the active Top, Bottom, or side-by-side
   view. Starting from Top or Bottom keeps that single-side view active.
6. Right-click to rotate clockwise by 45 degrees and left-click to place. The same
   footprint immediately remains ready with the next unused reference; press Escape
   to end the placement series.

Footprint calibration is available from **Image** → choose a side → edit image →
**Scale footprint**. Drag the yellow lower-right handle to resize; the top-left
corner remains fixed. Drag inside the footprint square to move it. The crop overlay
shows the kept area clearly and dims pixels outside it.

Placement creates every named footprint pad automatically. Pad identities combine
the reference and KiCad pad number (`U1.1`, `U1.2`, and so on). Repeated physical
shapes carrying the same KiCad number are grouped into one logical pad. Bottom-side
geometry is mirrored automatically. Footprint millimeter dimensions are converted
directly from the saved measurement line and its entered millimeter length, so
preview, outline, and pads remain at 1:1 physical scale through FIT and zoom changes.
The measurement field accepts either `30.5` or `30,5`. Legacy images that do not
contain both measurement values must be recalibrated once through **Edit image**.

The first implementation accepts standard rectangular, circular, oval, rounded
rectangle, and trapezoid pads. Unsupported custom pad shapes and malformed files are
rejected without modifying the project.

The selected `.kicad_mod` source is embedded in the `.revp` archive together with
the source revision. The global cache is therefore not required to reopen, display,
or save an existing project.

Shift-click a footprint (including one of its generated pads) to set the device
value or delete the complete device. Deleting a device removes its footprint
instance, all generated pads, and its embedded `.kicad_mod` asset. The BOM view
lists reference, component family, and value for every placed device.

KiCad library licensing information is retained in the downloaded cache. Consult
the repository's `LICENSE.md` before redistributing a library collection.
