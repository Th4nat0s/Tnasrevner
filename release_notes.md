# Release notes

## Unreleased

- Connections view now allows direct editing of pad net assignments with pin
  synchronization and validation.
- Added Top, Bottom, and side-by-side Top + Bottom controls to the board-photo
  calibration editor, with validated settings saved before switching faces.
- Stabilized the Application configuration menu lifetime and aligned regression
  coverage with configurable preview colors and canonical KiCad footprint assets.
- Crop rectangles now rotate with board photos and remain stable across repeated
  free-angle or quarter-turn adjustments.
- Adding or deleting board objects in the dual view now preserves the clicked
  side viewport and never resets or moves zoom and pan.
- Pressing or releasing Shift in Connect mode now preserves the board viewport;
  added `documentation/shortcuts.md` with common and mode-specific controls.
- Fixed saves after stale or missing KiCad footprint references by repairing them
  from shared definitions or the local KiCad cache.
- Fixed Save As recovery when a device references a missing legacy KiCad footprint path.
- Added reserved NC annotation mode for intentionally unconnected pads and pins;
  NC remains auditable in tables but is excluded from schematic electrical routing.
- Added an application Config tab with YAML-persisted, validated pad and connection colors.
- Highlight active connection board pads and schematic terminals in green, distinct
  from the blue pads already assigned to a net.
- Add Pad mode now shows a crosshair cursor on both the PCB view and image label,
  and restores the normal cursor when the mode ends.
- Added shared KiCad footprint definitions with SHA-256 identity, deduplicated
  `.revp` assets, shared runtime parsing cache, and automatic v1 migration.
- Replaced the generic Measure Tool icon with a visible ruler glyph (`📐`).
- Added explicit schematic layout optimization with persistent Glue/Unglue
  constraints, 90-degree rotation search, cable-obstacle avoidance, and
  peripheral placement for unconnected components; optimization runs in a
  worker with live progress feedback.
- Connect mode now shows persistent Shift/Escape guidance and a live temporary
  preview from the first selected pad or pin in board and schematic views.
- Added versioned `REVP0002` magic headers to new `.revp` archives, with
  validation and backward-compatible loading of legacy raw-ZIP projects.
- Refined image editing and toolbar UX: one Image action for Top/Bottom selection,
  icon-only controls with hover help, visible crop kept/outside areas, Add Footprint,
  Add Pad, and a dedicated floppy-disk Save icon.
- Added Load and Remove actions beside image sizing controls in the image editor.
- Completed minimal project lifecycle: create, save, close, reopen, and invalid
  `.revp` archive handling.
- Added explicit Close project action with unsaved-change protection.
- Added image replacement/removal controls and clear missing/corrupt asset errors.
- Completed board picture workspace views with Top, Bottom, side-by-side, Both,
  zoom/pan controls, persisted display state, and saved calibration lines.
- Added persisted rectangular pad creation from Tools, automatic P-naming,
  right-click net assignment, electrical-net connection lines, and a Nets table.
- Added rotating application diagnostics available through Tools > Log file.
- Added KiCad footprint caching and Add device placement with 45° right-click
  rotation, calibrated physical sizing, and automatically named footprint pads.
- Added first-run/monthly cache refresh, offline fallback, validated archive
  handling, and GUI coverage for KiCad cache failures.
- Fixed physical footprint sizing by retaining the exact calibration line and
  millimeter length; decimal input now accepts both dot and comma separators.
- Fixed footprint calibration handle alignment when the image is fitted or
  zoomed, preserving separate move and scale interactions; scaling now keeps
  the top-left corner fixed while the bottom-right handle moves.
- Added persistent five-item recent-footprint shortcuts, picker previews, and
  automatic per-family references such as `C1`, `C2`, and `R1`.
- Added a BOM view with editable device values and Shift-click actions to delete a
  complete KiCad device together with all generated pads.
- Device placement now continues with the same footprint and the next unique
  reference until Escape, while preserving an active Top or Bottom view.
- Added a configurable light color for glued schematic components and pads,
  with immediate Glue/Unglue visual updates.
- Through-hole KiCad footprints now create mirrored physical pad representations
  on both board faces while preserving one logical pin per pad number.
- Bottom-image calibration now shows the right-edge 180° flip convention, shared
  by THT mirroring and the corrected Top-oriented overlay rendering.
