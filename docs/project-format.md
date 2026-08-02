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

The ZIP is written atomically through a temporary file. Source pictures are read
and copied into the archive; original files are never modified.
