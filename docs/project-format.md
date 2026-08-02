# Project format

Minimal projects are directories containing `project.json`. Imported pictures
are stored later under the same directory, normally in `assets/`; metadata must
store paths relative to the project root so a complete project directory can be
moved or copied.

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
    {"side": "top", "path": "assets/top.png", "original_name": "top.png"}
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
