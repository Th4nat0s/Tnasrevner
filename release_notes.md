# Release notes

## Unreleased

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
  zoomed, preserving separate move and scale interactions.
- Added persistent five-item recent-footprint shortcuts, picker previews, and
  automatic per-family references such as `C1`, `C2`, and `R1`.
- Added a BOM view with editable device values and Shift-click actions to delete a
  complete KiCad device together with all generated pads.
- Device placement now continues with the same footprint and the next unique
  reference until Escape, while preserving an active Top or Bottom view.
