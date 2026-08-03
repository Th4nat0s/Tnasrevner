# Requirements Specification — Tnasrevner

## 1. Project purpose

Tnasrevner is a desktop application for reverse engineering electronic circuit boards.
It allows the user to document the components and pads visible on both sides of a
board, then reconstruct its logical electrical connectivity.

The goal is not to redraw the printed circuit board or reproduce the exact path of
each copper trace. The application must produce a usable electrical representation
and, eventually, generate the board's electronic schematic.

The target repository is:
`git@github.com:Th4nat0s/Tnasrevner.git`.

## 2. Supported platforms and general constraints

- Native desktop application for macOS and Linux.
- Primarily developed in Python.
- User interface designed for working with circuit board photographs.
- Projects can be saved, closed, and reopened later.
- Manual operation must not depend on automatic image recognition.

### 2.1 Product principles

- **Connectivity first:** the electrical netlist is the source of truth.
- **No PCB rerouting:** Tnasrevner records which pads are connected, not the exact
  geometry of the copper between them.
- **Manual work must always remain possible:** automation may assist the user later,
  but it must never prevent or invalidate manual editing.
- **Progress must be explicit:** unknown, connected, intentionally unconnected, and
  conflicting pads are different states.
- **Non-destructive editing:** imported source images are preserved and annotations
  are stored separately.
- **Portable projects:** moving or copying a complete project must not break its
  images or electrical data.
- **Incremental delivery:** every development milestone must leave a usable and
  testable application state.

### 2.2 Product terminology

- **Board:** the physical electronic circuit board being analyzed.
- **Side:** an externally visible surface, either top or bottom.
- **Layer:** a physical copper layer, numbered from L1 to L4.
- **Device:** a physical component placed on an external side of the board.
- **Pad:** an electrical contact belonging to a device or existing independently.
- **Via:** a plated connection between specified copper layers.
- **Net:** a logical electrical connection shared by one or more pads or vias.
- **Plane:** an internal layer associated with a net such as GND or POWER.
- **Reverse-engineered pad:** a pad whose connection state is known and validated.

## 3. Project management

The application must provide a project manager that supports at least the following:

- create a project;
- name the project and its board;
- open an existing project;
- save images, components, pads, nets, and display settings;
- display reverse-engineering progress;
- configure the board with one to four layers.

### 3.1 Project initialization workflow

Creating a project must start a guided initialization phase in this order:

1. enter the project and board names;
2. select the total number of board layers;
3. import the available external-side images: top first, then bottom when applicable;
4. immediately request board calibration after the image-import step;
5. validate the initialization and open the main editing workspace.

Calibration is therefore part of project initialization, not a later editing task.
The application must not ask for physical dimensions before the external images have
been imported. If initialization is interrupted, the project must preserve its state
and resume at the first incomplete step when reopened.

## 4. Layer model

Only the external board sides can have an imported image. Internal layers do not have
an image and are primarily used to represent ground and power planes.

Layer numbering depends on the total number of layers:

| Layer count | Layer assignment |
|---:|---|
| 1 | L1: top |
| 2 | L1: top, L2: bottom |
| 3 | L1: top, L2: internal, L3: bottom |
| 4 | L1: top, L2: internal GND, L3: internal POWER, L4: bottom |

The names and functions of internal layers must be editable. When a board changes
from two to four layers, the side previously identified as L2 becomes L4.

## 5. Board-side images

The user must be able to import:

- one image of the top side;
- one image of the bottom side, when that side is accessible.

No image is imported for an internal layer.

The application must provide three display modes:

1. top only;
2. bottom only;
3. top and bottom side by side.

A **Flip Board** button must provide quick switching between the top and bottom
sides. The opposite side can be mirrored to preserve the physical correspondence
between positions. Horizontal mirroring is the default, with an option for vertical
mirroring.

In side-by-side mode:

- both images are visible at the same time;
- zooming and panning can be synchronized;
- the same physical area must remain identifiable on both sides;
- synchronization can be temporarily disabled.

## 6. Board calibration and measurements

The board workspace must be calibrated so image coordinates can be converted into
real physical measurements. This calibration must be requested immediately after
external-side image import, during the guided project initialization phase and before
normal device placement begins.

The calibration workflow must allow the user to:

1. define the usable board outline on the image;
2. enter the board's real length and width;
3. select the measurement unit, with millimeters as the default;
4. confirm the orientation of the length and width axes;
5. recalibrate the board later without losing placed devices or pads.

After calibration, the application must be able to express device positions, pad
positions, distances, and dimensions in millimeters. Both external images must share
the same physical coordinate system so corresponding positions remain aligned when
the board is flipped or shown side by side.

## 7. Device identification and creation workflow

The user must be able to draw a rectangular area around every device visible in an
image.

Each device must support at least:

- a reference designator, such as `R1`, `C3`, `U2`, or `Q1`;
- its board side;
- its pads;
- later, a type, value, package, and electrical symbol.

When a device is added, the application must follow this workflow:

1. the user draws the device's rectangular bounding box;
2. the application asks for its reference designator and number of pads;
3. after confirmation, the application enters pad-placement mode;
4. the user places the requested pads one by one on the board image;
5. pads are numbered automatically in placement order, starting at pad 1;
6. the device remains visibly incomplete until every requested pad has been placed.

The device area, reference designator, declared pad count, and individual pad
positions must remain editable after creation. The user must be able to cancel the
operation or correct the most recently placed pad without deleting the entire device.

### 7.1 Quick placement for simple SMD devices

The application must provide a faster placement workflow for common two-terminal SMD
devices, initially:

- SMD resistors;
- SMD capacitors.

When one of these tools is selected, the application must create a two-pad device
without asking the user to enter the pad count. It must automatically:

- assign the appropriate reference-designator prefix (`R` or `C`);
- create pads 1 and 2;
- allow the user to position, rotate, and resize the device on the image;
- allow both pad positions to be adjusted manually;
- request or allow later entry of the component value and package.

The regular device workflow remains available for all other device types and for
unusual resistor or capacitor packages.

## 8. Pad identification

The user must be able to place pads on the images and associate them with a device
when applicable. During device creation, the application must guide the user until
the declared number of pads has been placed. Pads must be identifiable and numbered
so physical pads can be mapped to the pins of a future electrical symbol.

Each pad has a visual state. The proposed color code is:

| Color | State |
|---|---|
| Orange | Connection unknown; the pad still needs to be investigated |
| Green | Pad assigned to a net; its connection has been reverse-engineered |
| Gray | Pad confirmed as intentionally not connected (`NC`) |
| Purple | Pad connected to an internal plane, such as GND or POWER |
| Blue | Pad currently selected or hovered |
| Red | Error or conflict requiring review |

The distinction between an unprocessed pad and a genuinely unconnected pad is
mandatory. It enables the application to calculate reliable project progress.

## 9. Creating connections

A connection palette or tool must allow the user to:

- select multiple pads;
- group them into the same electrical net;
- optionally assign a name to the net, such as `GND`, `+5V`, or `NET_023`;
- edit or remove an incorrectly created connection;
- assign a pad to an internal layer or plane.

The stored connection is logical. It does not represent the physical routing of a
copper trace on the printed circuit board.

## 10. Net visualization

Connection lines must not permanently clutter the board view.

When the user hovers over a pad:

- every pad belonging to the same net is highlighted;
- temporary straight lines show their relationships;
- the net name may be displayed;
- large nets use the minimum useful number of lines to avoid an unreadable web.

In side-by-side mode, a connection can be visualized across the two board sides.
These lines are only a visual aid: they have no trace width, curvature, or physical
manufacturing meaning.

## 11. Internal layers and electrical planes

Internal layers are represented logically, without photographs or PCB artwork. They
are primarily used to associate pads or vias with nets such as `GND`, `VCC`, `+5V`,
or `+3V3`.

For a four-layer board, the application proposes the following defaults:

- L2: GND plane;
- L3: POWER plane.

These defaults remain editable to match the board being analyzed.

## 12. Electrical output

Validated connectivity must form a reliable netlist, independently of how temporary
connection lines are displayed.

Generating a complete electronic schematic will also require:

- associating components with electrical symbols;
- mapping physical pads to symbol pins;
- placing symbols;
- arranging wires and net labels into a readable schematic;
- a manual review and correction step.

The V1 target is to generate an editable electronic schematic from the
reverse-engineered data. The exact schematic format remains to be formally approved.
KiCad is the recommended first target because it provides an existing schematic
editor and avoids building a second complete schematic editor inside Tnasrevner.

## 13. Progress tracking and validation

The project manager must be able to report:

- total number of devices and pads;
- devices whose declared pads have not all been placed;
- number of processed pads;
- number of pads still requiring investigation;
- number of pads confirmed as NC;
- errors or conflicts;
- overall completion percentage.

A project must not be considered fully reverse-engineered while any pads remain in
the “connection unknown” state or any conflicts remain unresolved.

## 14. Current exclusions

The following features are outside the currently defined scope:

- reproducing the physical routing of copper traces;
- generating PCB artwork or manufacturing files;
- managing trace width, shape, or impedance;
- importing images of internal layers;
- replacing a PCB design tool such as KiCad.

## 15. Decisions still required

- Method for aligning the top and bottom images.
- Via management and representation.
- Exact behavior for a net containing more than two pads.
- Electrical symbol library and symbol creation workflow.
- Generated schematic format: internal format, KiCad, or another format.
- Supported image formats and resolution limits.
- Supported standard SMD package sizes and their default pad geometry.
- Netlist and project-data export formats.
- Undo/redo and change-history requirements.
- Potential automatic image-processing assistance.

The following decision is blocking for the final V1 output milestone but does not
block the project editor, calibration, device, pad, or net milestones:

- approval of KiCad, an internal schematic format, or another editable schematic
  format as the first supported target.

## 16. Core user journey

The intended end-to-end workflow is:

1. the user creates a project and identifies the board;
2. the user selects the board's layer count;
3. the user imports the top and, when applicable, bottom photographs;
4. the initialization wizard requests board calibration;
5. the main workspace opens in top, bottom, or side-by-side mode;
6. the user draws devices and places their pads;
7. common two-pad SMD resistors and capacitors use a faster placement tool;
8. the user selects pads and groups them into electrical nets;
9. hovering over a pad temporarily reveals its net using straight lines;
10. colors and progress indicators show which pads still require investigation;
11. the user resolves unknown pads and conflicts;
12. Tnasrevner validates and exports the netlist;
13. Tnasrevner maps devices and pins to symbols and generates an editable schematic;
14. the user reviews and corrects the generated schematic in the chosen editor.

## 17. Proposed project data model

The persisted project model should contain at least:

- project identity, board name, timestamps, format version, and initialization state;
- total layer count and editable layer definitions;
- imported image metadata and relative asset paths;
- calibration dimensions and image-to-board coordinate transformations;
- view orientation, mirroring, synchronization, zoom, and display preferences;
- device identity, reference, type, value, package, side, bounding box, and rotation;
- declared pad count and individual pad number, position, shape, and state;
- via position and layer span once the via workflow is approved;
- net identity, optional name, and membership;
- internal plane-to-net assignments;
- electrical symbol choice and pad-to-pin mapping;
- validation issues and progress statistics.

Board-space coordinates should use millimeters. Image pixels remain source-display
coordinates and are converted through calibration transformations. Top and bottom
images must resolve into a common board-space coordinate system.

Project data requires an explicit format version so future application releases can
migrate older projects safely.

## 18. Recommended technical architecture

The recommended implementation stack is:

- Python 3.11 or newer;
- PySide6 and Qt 6 for the macOS/Linux desktop interface;
- Qt's 2D graphics framework or an equivalent scene-based canvas for large images,
  overlays, selection, zooming, and panning;
- SQLite for transactional structured project data;
- a project-local `assets` directory for imported images;
- optional OpenCV adapters for later alignment and image-processing assistance;
- PyInstaller or an equivalent packaging system, built separately on macOS and Linux;
- pytest for automated tests and CI builds for both operating systems.

The application should be divided into four boundaries:

1. **Domain:** devices, pads, layers, nets, planes, calibration, and validation rules.
2. **Application:** project initialization and editing use cases.
3. **Infrastructure:** SQLite persistence, files, migrations, import, and export.
4. **UI:** project manager, initialization wizard, board canvas, palettes, and dialogs.

The domain and application layers must not depend on the GUI. This keeps the
electrical rules testable and allows future CLI or automation tools to reuse them.

## 19. V1 delivery roadmap

Development should proceed through the following ordered milestones. Each milestone
must be demonstrated and accepted before dependent work becomes the main focus.

### 19.1 Requirements and UX definition

- approve the V1 scope and remaining product decisions;
- produce simple wireframes for initialization and the main workspace;
- approve the project schema and coordinate model;
- choose the first editable schematic output format.

**Deliverable:** approved requirements, wireframes, and technical decision record.

### 19.2 Application foundation

- establish the Git repository and Python/PySide6 structure;
- add coding standards, tests, and macOS/Linux continuous integration;
- implement the application shell, navigation, and error reporting;
- create project-format versioning and migration foundations.

**Deliverable:** an empty but installable, testable application on both platforms.

### 19.3 Project initialization

- implement the project manager;
- implement layer selection and external-image import;
- implement the mandatory post-import calibration step;
- persist and resume interrupted initialization;
- create, save, close, and reopen portable projects.

**Deliverable:** a calibrated project survives a complete close-and-reopen cycle.

### 19.4 Visual board workspace

- display high-resolution source images;
- implement zoom, pan, top, bottom, and side-by-side modes;
- implement board flipping and horizontal or vertical mirroring;
- synchronize the two views and allow temporary desynchronization;
- verify pixel-to-millimeter conversion and shared coordinates.

**Deliverable:** reliable navigation of corresponding physical areas on both sides.

### 19.5 Devices and pads

- implement device bounding-box creation and editing;
- request a reference and pad count;
- guide numbered pad placement and show incomplete devices;
- implement move, resize, rotate, correct, cancel, and delete operations;
- implement quick two-pad SMD resistor and capacitor placement.

**Deliverable:** the visible board population can be documented and restored.

### 19.6 Electrical connectivity

- implement single and multiple pad selection;
- implement the connection palette and net creation;
- support net naming, editing, merging, splitting, and deletion;
- support NC confirmation and internal-plane assignment;
- implement the approved via model;
- enforce state transitions and conflict rules.

**Deliverable:** the board's logical connectivity can be reconstructed manually.

### 19.7 Visualization, validation, and progress

- implement pad-state colors;
- reveal and highlight a net on hover;
- draw temporary straight connections with readable behavior for large nets;
- visualize cross-side connections;
- detect unknown pads, incomplete devices, invalid mappings, and conflicts;
- calculate progress and provide a final validation report.

**Deliverable:** the user can inspect completeness and resolve reported issues.

### 19.8 Netlist and schematic output

- implement deterministic JSON and CSV diagnostic exports;
- implement the chosen formal netlist export;
- implement the electrical-symbol and pad-to-pin mapping workflow;
- generate an editable schematic in the approved output format;
- report devices or mappings that prevent complete schematic generation.

**Deliverable:** a validated netlist and editable schematic generated from the project.

### 19.9 Stabilization and distribution

- add autosave, undo/redo, and recovery behavior;
- test large images and realistic boards;
- test project-format upgrades and corrupted inputs;
- perform macOS and Linux usability tests;
- write user documentation;
- build and verify distributable application packages.

**Deliverable:** a documented and distributable V1 release.

## 20. Quality and verification strategy

- Unit-test domain rules, state transitions, progress, and validation.
- Integration-test project creation, persistence, migrations, and exports.
- Test coordinate transformations using known calibration points.
- Test save/reopen round trips without data or precision loss.
- Test UI workflows for initialization, device creation, pad placement, and nets.
- Maintain representative one-, two-, three-, and four-layer sample projects.
- Verify high-resolution images without unacceptable navigation latency.
- Build and smoke-test packages on both macOS and Linux.
- Never consider a generated schematic valid when required pad-to-pin mappings are
  missing; surface an explicit blocking report instead.

## 21. First functional MVP acceptance criteria

The first MVP is considered usable when a user can:

1. create and reopen a project;
2. configure a board with one to four layers;
3. import images of the external board sides;
4. calibrate the workspace using the board's real length and width;
5. display the top, the bottom, or both sides side by side;
6. flip between sides and configure their orientation;
7. draw device bounding boxes and assign reference designators;
8. declare the number of pads for a device, then place and identify every pad;
9. quickly place a two-pad SMD resistor or capacitor;
10. select multiple pads to create a net;
11. immediately recognize pad states through the color code;
12. hover over a pad to display its connections as straight lines;
13. save and restore all completed work;
14. obtain a netlist of the documented connectivity.

This functional MVP proves the central editing and connectivity workflow. It is not
the complete V1 release because schematic generation, stabilization, and packaging
still follow.

## 22. V1 completion criteria

V1 is complete when all functional MVP criteria are satisfied and:

1. device symbols and physical-pad-to-symbol-pin mappings can be defined;
2. a validated project produces an editable electronic schematic;
3. incomplete mappings produce a clear blocking report rather than incorrect output;
4. autosave and undo/redo protect normal editing work;
5. existing projects can be migrated when the stored format changes;
6. the application has passed representative macOS and Linux tests;
7. installable packages and basic user documentation are available;
8. no known issue can silently corrupt project connectivity.

## 23. Post-V1 candidates

The following ideas should be evaluated only after the manual V1 workflow is stable:

- assisted top/bottom image alignment;
- automatic pad or device detection;
- computer-vision suggestions for likely connections;
- additional predefined SMD and through-hole packages;
- more schematic and netlist export formats;
- collaboration and change-review features;
- measurement, reporting, and annotated-image exports.

## 24. Current implementation checkpoint — 2026-08-03

### Delivered

- Portable `.revp` project lifecycle with image assets, display state, pads, nets,
  devices, BOM, and embedded KiCad footprint sources.
- Top/bottom image workspace with zoom, pan, fit, centering, side-by-side, overlay,
  rotation, crop editing, line calibration, and footprint-based physical sizing.
- Consolidated image workflow: one Image action selects Top/Bottom; the editor
  provides Load, Remove, Scale line, Scale footprint, Crop, rotation, zoom, and FIT.
- Visible crop mask: kept pixels remain clear, excluded pixels are dimmed.
- KiCad official-footprint cache with first-run download, 30-day refresh, atomic
  replacement, metadata, validation, and offline fallback.
- Manual pad/net workflow, footprint placement, generated pads, net visualization,
  recent footprint picker entries, and BOM values.

### Remaining before MVP

- Layer count/model and board-level physical dimensions.
- Resumable project initialization wizard.
- Pad-state colors, explicit NC/plane states, progress, conflict validation.
- Complete net editing model, vias, netlist export, and schematic generation.
- Autosave, undo/redo, migration support (`REVP0000` tracked by issue #8), CI, and
  packaging.

### Evidence

- 73 automated tests passing.
- GUI/KiCad code formatted with Black and rated 10/10 by Pylint.
- Open work is tracked in GitHub issue #8; issues #6 and #7 are closed.
