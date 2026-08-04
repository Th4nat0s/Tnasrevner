# Development Rules — Tnasrevner

## Platform compatibility

- The application must run on macOS 15.6 or newer.
- macOS 15.6 is the minimum supported macOS version and must be included in
  compatibility checks before a release.
- Python support must follow the project specification: Python 3.11 or newer.
- Do not use APIs, dependencies, or platform features that silently require a
  newer macOS version without documenting and testing that requirement.
- Keep the application portable to Linux where practical, but macOS 15.6 is the
  mandatory baseline.

## Code quality

- Format all Python code with Black. Code that fails the Black check must not be
  considered complete.
- Run Pylint on all production Python code and fix every actionable finding.
- The minimum accepted quality score is 8/10. No new or modified code may reduce
  the project quality score below 8/10.
- Prefer small, explicit, testable functions and clear domain models.
- Keep the domain and application layers independent from PySide6 and other UI
  implementation details.
- Avoid duplicated logic, hidden global state, unexplained constants, and broad
  exception handling.
- Every function and method in every `.py` file is mandatory documentation and
  typing scope: production code, tests, scripts, helpers, private functions,
  callbacks, and overrides are included without exception.
- Every function and method must have complete type annotations for parameters
  and return value, including `None` where applicable.
- Every function and method must have a docstring describing its purpose,
  every call parameter, the return value, and raised exceptions when relevant.
- Parameter and return descriptions must stay accurate when the signature or
  behavior changes; do not use vague placeholder docstrings.
- Code that adds or modifies a function without this documentation and typing
  is incomplete and must not be handed off.

## Regression protection

- Never knowingly introduce a regression in an existing workflow, file format,
  migration, calculation, export, or user-visible behavior.
- Before changing persisted data, define the migration path and test old project
  files as well as newly created projects.
- Preserve source images and existing project data during edits and migrations.
- Undo/Redo and component, pad, image, or net modifications must preserve the
  current tab, zoom, pan position, and schematic scroll position.
- Re-run the complete relevant test suite after every functional change.
- A change is incomplete if a failing test is ignored, weakened, deleted, or made
  less meaningful instead of fixing the underlying problem.

## Testing requirements

- Test business logic extensively with unit tests, including valid, invalid,
  incomplete, conflicting, and boundary cases.
- Add functional tests for every user workflow that is implemented or changed.
- Functional coverage must include project creation, initialization resume,
  image import, calibration, layer configuration, device creation, pad placement,
  net editing, validation, save/reopen, and export where applicable.
- Test complete workflows, not only isolated methods. Prefer tests that exercise
  the same application boundaries used by the real user interface.
- Test one-, two-, three-, and four-layer boards where the behavior differs.
- Test coordinate transformations with known calibration points and verify that
  top/bottom views share the same board-space coordinates.
- Test persistence round trips, migrations, corrupted input, duplicate data,
  incomplete devices, unknown pads, NC pads, conflicts, and invalid mappings.
- Add a regression test for every bug fixed.
- Do not claim completion when the relevant tests, lint checks, or formatting
  checks have not passed.

## Required verification before hand-off

Run, as applicable:

```bash
black --check .
pylint src tests
pytest -q
```

For UI changes, also run the functional/UI test suite on macOS 15.6 or newer.
For platform-sensitive changes, verify the behavior on the minimum supported
macOS version before release.

## Delivery rules

- Every milestone must leave the application usable and testable.
- Keep changes focused and explain important architectural decisions in the
  documentation or project decision record.
- Do not replace manual workflows with automation that prevents manual editing.
- Do not mark a feature complete until its behavior, persistence, validation,
  error handling, and regression tests are covered.
- Report any unverified platform, test, or quality requirement explicitly.
