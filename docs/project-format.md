# Project format

Projects are single `.revp` ZIP files. Each archive contains `project.json` and
imported pictures under `assets/`. Metadata stores paths relative to the archive
root, making one `.revp` file sufficient to move or copy a project.

`format_version` is required. Current version is `1`.

```json
{
  "format_version": 1,
  "project_id": "uuid",
  "project_name": "Example project",
  "board_name": "Example board",
  "created_at": "2026-08-02T12:00:00+00:00",
  "updated_at": "2026-08-02T12:00:00+00:00",
  "images": [
    {"side": "top", "path": "assets/top.png", "original_name": "top.png", "pixels_per_mm": 12.5}
  ],
  "pads": [
    {"pad_id": "uuid", "name": "P1", "side": "top", "x": 0.1, "y": 0.2, "width": 0.05, "height": 0.04, "net": "GND"}
  ],
  "display": {
    "mode": "top",
    "zoom": 1.0,
    "pan_x": 0.0,
    "pan_y": 0.0,
    "synchronized": true
  }
}
```

`images` supports zero, one top, one bottom, or one image for each side. Paths
must be relative, use `/` separators, and cannot escape the project root.
Unknown future fields are ignored when reading; unsupported format versions are
rejected explicitly.

Display mode `both` overlays the top image with the bottom image mirrored
horizontally. This is a view preference only; source images remain unchanged.

Imported images store `pixels_per_mm`, measured from the mandatory calibration
line drawn during import. This value is optional for older projects.

Pads keep a stable unique name such as `P1`. The optional `net` field assigns
electrical connectivity; pads sharing a net are connected in the workspace.

The ZIP is written atomically through a temporary file. Source pictures are read
and copied into the archive; original files are never modified.
