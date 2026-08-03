# Current project status

Status date: 2026-08-03

## Working today

- Portable `.revp` projects: create, load, save, close, reopen, and missing/corrupt
  asset reporting.
- Top/bottom board images with side-by-side and overlay views, zoom, pan, fit, and
  center controls.
- Image editor with rotation, crop overlay, visible kept/outside-crop areas, line
  calibration, and KiCad-footprint calibration.
- Image toolbar consolidated into one **Image** action: choose Top or Bottom, then
  load a missing image or edit an existing one. The editor exposes **Load** and
  **Remove** beside sizing controls.
- Manual pad creation, pad visibility, net assignment, connection visualization,
  BOM, and device deletion.
- Component pin records with editable pin ID/function and pad-to-pin net links.
- KiCad footprint cache from the official repository, monthly refresh, atomic
  replacement, offline fallback, validated parsing, footprint picker, and physical
  placement.
- Toolbar uses icon-only actions with hover tooltips: image, centering, pads,
  Add Footprint, Add Pad, save, log, and quit.

## Not complete yet

- One-to-four layer configuration and editable internal planes.
- Full board outline/length/width calibration and resumable initialization wizard.
- Pad state workflow: unknown, connected, NC, plane, selected, conflict, and
  progress reporting.
- Via model, advanced net editing, conflict validation, netlist export, and
  schematic generation.
- Autosave, undo/redo, migration header `REVP0000` (tracked in issue #8), CI, and
  distributable packages.

## Verification

The current automated suite has 73 passing tests. GUI and KiCad code pass Black and
Pylint checks. Tests cover project round trips, image workflows, footprint parsing,
cache freshness/failure behavior, pad/net operations, and footprint placement.

## Next recommended milestones

1. Freeze and document the `.revp` header/version migration policy (#8).
2. Add explicit board/layer model before extending connectivity semantics.
3. Add pad-state/progress validation, then netlist export.
4. Add autosave/undo recovery before large-board usability work.
