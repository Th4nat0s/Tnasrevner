# KiCad footprints

Tnasrevner uses the official KiCad footprint repository:

`https://gitlab.com/kicad/libraries/kicad-footprints`

The tracked ref is `master`, currently intended for KiCad 10. Each successful
download records the exact Git commit, source URL, ref, and UTC update time.

## Cache lifecycle

The application prepares the cache after startup without blocking the GUI. It also
checks it before Add device opens. A valid cache younger than 30 days is reused.
At 30 days or older, Tnasrevner attempts an atomic replacement from a new repository
snapshot.

The cache is stored under Qt's application-data directory in `kicad-footprints/`:

- macOS: normally `~/Library/Application Support/Tnasrevner/kicad-footprints`;
- Linux: normally `~/.local/share/Tnasrevner/kicad-footprints`.

If an update fails, an existing valid cache remains available and the application
shows a warning. If the first download fails, Add device reports an actionable error
without changing the project. A corrupt cache can be recovered by removing only the
`kicad-footprints` cache directory and starting Tnasrevner again.

Archives are downloaded into a temporary directory, checked for unsafe paths, and
validated before replacing the current cache. Tests inject local archives and never
require network access.

## Add device workflow

1. Import and calibrate at least one board image.
2. Select **Add device** in Tools and enter a unique reference such as `U1`.
3. Search for and select a cached KiCad footprint.
4. Move the physically scaled preview over the Top or Bottom image.
5. Right-click to rotate clockwise by 45 degrees; left-click to place; press Escape
   to cancel.

Placement creates every named footprint pad automatically. Pad identities combine
the reference and KiCad pad number (`U1.1`, `U1.2`, and so on). Repeated physical
shapes carrying the same KiCad number are grouped into one logical pad. Bottom-side
geometry is mirrored automatically.

The first implementation accepts standard rectangular, circular, oval, rounded
rectangle, and trapezoid pads. Unsupported custom pad shapes and malformed files are
rejected without modifying the project.

The selected `.kicad_mod` source is embedded in the `.revp` archive together with
the source revision. The global cache is therefore not required to reopen, display,
or save an existing project.

KiCad library licensing information is retained in the downloaded cache. Consult
the repository's `LICENSE.md` before redistributing a library collection.
