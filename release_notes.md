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
- Fixed physical footprint sizing by retaining the exact calibration line and
  millimeter length; decimal input now accepts both dot and comma separators.
